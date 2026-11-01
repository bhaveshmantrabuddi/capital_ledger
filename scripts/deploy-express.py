#!/usr/bin/env python3
"""Plan or deploy a NEW app stack referencing an existing Aurora Express database.

The default is read-only. --apply publishes new resources after the identity,
Free plan, database, and ownership guards pass. No account-plan upgrade or
existing manual-resource adoption is performed. Requires AWS CLI v2 and pip.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_REQUIREMENTS = (
    'psycopg==3.3.6', 'psycopg-binary==3.3.6', 'typing_extensions==4.16.0')
OWNER_TAGS = {'Application': 'CapitalLedger', 'DeploymentMode': 'ExpressExternal'}
PUBLIC_BLOCK = {k: True for k in (
    'BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')}


class DeploymentError(RuntimeError):
    """A guard failed; command output never includes credentials or auth tokens."""


class AwsError(DeploymentError):
    def __init__(self, service, operation, code):
        self.code = code
        super().__init__(f'AWS {service} {operation} failed ({code}). Check the selected profile and permissions.')


class Aws:
    def __init__(self, profile, region):
        self.executable = shutil.which('aws')
        if not self.executable:
            raise DeploymentError('Install current AWS CLI v2 and sign in to the named profile.')
        self.base = ['--profile', profile, '--region', region, '--no-cli-pager', '--no-cli-auto-prompt']

    def call(self, *args):
        # A subprocess argument list is intentional: never interpolate a shell command.
        env = dict(os.environ, AWS_PAGER='', AWS_CLI_AUTO_PROMPT='off')
        result = subprocess.run([self.executable, *args, *self.base, '--output', 'json'],
                                capture_output=True, text=True, env=env, check=False)
        if result.returncode:
            match = re.search(r'\(([A-Za-z0-9_.-]+)\)', result.stderr)
            code = match.group(1) if match else 'CLIError'
            # Some S3 HeadBucket errors use just (404), still distinguishable here.
            raise AwsError(args[0], args[1], code)
        if args[:2] == ('cloudformation', 'deploy'):
            # AWS CLI's deploy custom command prints progress, even with --output json.
            return {}
        try:
            return json.loads(result.stdout) if result.stdout.strip() else {}
        except ValueError as exc:
            raise DeploymentError(f'AWS {args[0]} {args[1]} returned invalid JSON.') from exc


@dataclass(frozen=True)
class Config:
    account: str
    region: str
    profile: str
    cluster: str
    stack: str = 'capital-ledger-express'
    prefix: str = 'capital-ledger-express'
    artifact_bucket: str = ''
    domain_prefix: str = ''
    username: str = 'postgres'
    database: str = 'postgres'
    development_origin: str = ''
    daily_recovery: bool = True

    def validate(self):
        patterns = {
            'account': r'[0-9]{12}', 'region': r'(?:us|eu|ap|ca|sa|af|me|il|mx)-[a-z]+-\d+',
            'profile': r'[A-Za-z0-9_.@-]+', 'cluster': r'[A-Za-z][A-Za-z0-9-]{0,62}',
            'stack': r'[A-Za-z][A-Za-z0-9-]{2,63}', 'prefix': r'[a-z][a-z0-9-]{2,31}',
            'username': r'[A-Za-z_][A-Za-z0-9_]*', 'database': r'[A-Za-z_][A-Za-z0-9_]*',
            'domain_prefix': r'|[a-z0-9][a-z0-9-]{2,62}',
            'artifact_bucket': r'|[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',
            'development_origin': r'|http://(?:localhost|127\.0\.0\.1):[0-9]{2,5}',
        }
        for name, pattern in patterns.items():
            if not re.fullmatch(pattern, getattr(self, name)):
                raise DeploymentError(f'Invalid {name.replace("_", " ")}.')
        if self.development_origin and not 1 <= int(self.development_origin.rsplit(':', 1)[1]) <= 65535:
            raise DeploymentError('Development origin port is invalid.')
        if self.stack == 'capital-ledger' or self.prefix == 'capital-ledger':
            raise DeploymentError('Choose a distinct new stack/prefix; capital-ledger is reserved for the manual deployment.')


def require_free_identity(config, aws):
    identity = aws.call('sts', 'get-caller-identity')
    if identity.get('Account') != config.account:
        raise DeploymentError('The signed-in AWS account does not match --expected-account. No resources changed.')
    plan = aws.call('freetier', 'get-account-plan-state')
    if (plan.get('accountId') != config.account or plan.get('accountPlanType') != 'FREE'
            or plan.get('accountPlanStatus') != 'ACTIVE'):
        raise DeploymentError('An ACTIVE AWS Free plan for the expected account must be verified. Do not upgrade to bypass this guard.')
    credits = plan.get('accountPlanRemainingCredits', {})
    try:
        amount = float(credits['amount'])
    except (KeyError, TypeError, ValueError) as exc:
        raise DeploymentError('Free plan credit balance could not be verified.') from exc
    if not math.isfinite(amount) or amount <= 0 or credits.get('unit') != 'USD':
        raise DeploymentError('Available USD Free plan credits must be verified before resource creation.')
    return plan


def existing_cluster(config, aws):
    result = aws.call('rds', 'describe-db-clusters', '--db-cluster-identifier', config.cluster)
    clusters = result.get('DBClusters', [])
    if len(clusters) != 1:
        raise DeploymentError('Exactly one existing Aurora Express cluster is required.')
    db = clusters[0]
    expected_arn = f'arn:aws:rds:{config.region}:{config.account}:cluster:{config.cluster}'
    checks = (
        (db.get('DBClusterArn') == expected_arn, 'Cluster must belong to the expected account and region.'),
        (db.get('DBClusterIdentifier') == config.cluster, 'Cluster identifier does not match.'),
        (db.get('Engine') == 'aurora-postgresql', 'Aurora PostgreSQL is required.'),
        (db.get('Status') == 'available', 'The Aurora cluster must be available.'),
        (db.get('IAMDatabaseAuthenticationEnabled') is True, 'IAM database authentication must already be enabled.'),
        (db.get('InternetAccessGatewayEnabled') is True, 'This path requires Aurora Express internet access gateway connectivity.'),
        (db.get('Port') == 5432, 'The Express PostgreSQL port must be 5432.'),
        (db.get('ServerlessV2ScalingConfiguration', {}).get('MinCapacity') == 0,
         'The existing cluster must support auto-pause with MinCapacity 0.'),
        (bool(re.fullmatch(r'cluster-[A-Za-z0-9]+', db.get('DbClusterResourceId', ''))),
         'The authoritative Aurora cluster resource ID is missing.'),
        (bool(re.fullmatch(r'[a-zA-Z0-9.-]+\.' + re.escape(config.region) + r'\.rds\.amazonaws\.com',
                           db.get('Endpoint', ''))), 'The authoritative Aurora endpoint is invalid or in another region.'),
    )
    for valid, message in checks:
        if not valid:
            raise DeploymentError(message + ' No database settings were changed.')
    return db


def lookup(aws, args, missing_codes):
    try:
        return aws.call(*args)
    except AwsError as exc:
        if exc.code in missing_codes:
            return None
        raise


def check_stack_ownership(config, aws):
    result = lookup(aws, ('cloudformation', 'describe-stacks', '--stack-name', config.stack), {'ValidationError'})
    if result is None:
        # Named resources must not silently adopt a manual deployment's prefix.
        for suffix in ('customers', 'transactions', 'bootstrap', 'maintenance'):
            existing = lookup(aws, ('lambda', 'get-function-configuration', '--function-name',
                                   f'{config.prefix}-{suffix}'), {'ResourceNotFoundException'})
            if existing is not None:
                raise DeploymentError('The requested prefix is already in use. Choose a new stack/prefix; manual functions are not adopted.')
        table = lookup(aws, ('dynamodb', 'describe-table', '--table-name', f'{config.prefix}-customers'),
                       {'ResourceNotFoundException'})
        if table is not None:
            raise DeploymentError('The requested customer table already exists outside this stack. Choose a new prefix.')
        role = lookup(aws, ('iam', 'get-role', '--role-name', f'{config.prefix}-maintenance-role'), {'NoSuchEntity'})
        if role is not None:
            raise DeploymentError('The requested maintenance role already exists. Choose a new prefix.')
        for suffix in ('outbox', 'outbox-dead-letter'):
            queue = lookup(aws, ('sqs', 'get-queue-url', '--queue-name', f'{config.prefix}-{suffix}',
                                '--queue-owner-aws-account-id', config.account),
                           {'AWS.SimpleQueueService.NonExistentQueue', 'QueueDoesNotExist'})
            if queue is not None:
                raise DeploymentError('The requested queue already exists outside this stack. Choose a new prefix.')
        names = {f'/aws/lambda/{config.prefix}-{suffix}' for suffix in
                 ('customers', 'transactions', 'bootstrap', 'maintenance')}
        groups = aws.call('logs', 'describe-log-groups', '--log-group-name-prefix',
                          f'/aws/lambda/{config.prefix}-').get('logGroups', [])
        if any(group.get('logGroupName') in names for group in groups):
            raise DeploymentError('An application log group already exists outside this stack. Choose a new prefix.')
        return False
    stacks = result.get('Stacks', [])
    if len(stacks) != 1:
        raise DeploymentError('The target stack could not be identified unambiguously.')
    stack = stacks[0]
    tags = {t['Key']: t['Value'] for t in stack.get('Tags', [])}
    params = {p['ParameterKey']: p.get('ParameterValue') for p in stack.get('Parameters', [])}
    if any(tags.get(k) != v for k, v in OWNER_TAGS.items()) or params.get('ResourcePrefix') != config.prefix:
        raise DeploymentError('The target stack is not owned by this Express deployment path. No manual stack is adopted.')
    if stack.get('StackStatus') not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'):
        raise DeploymentError('Finish or resolve the existing stack operation before redeploying.')
    return True


def check_bucket(config, aws, bucket):
    head = lookup(aws, ('s3api', 'head-bucket', '--bucket', bucket, '--expected-bucket-owner', config.account),
                  {'404', 'NoSuchBucket', 'NotFound'})
    if head is None:
        return False
    location = aws.call('s3api', 'get-bucket-location', '--bucket', bucket,
                        '--expected-bucket-owner', config.account).get('LocationConstraint') or 'us-east-1'
    if location != config.region:
        raise DeploymentError('The artifact bucket must be in the selected region.')
    block = aws.call('s3api', 'get-public-access-block', '--bucket', bucket,
                     '--expected-bucket-owner', config.account).get('PublicAccessBlockConfiguration', {})
    if any(block.get(k) is not True for k in PUBLIC_BLOCK):
        raise DeploymentError('An existing artifact bucket must have all four S3 public access blocks enabled.')
    tags = aws.call('s3api', 'get-bucket-tagging', '--bucket', bucket,
                    '--expected-bucket-owner', config.account).get('TagSet', [])
    tags = {t['Key']: t['Value'] for t in tags}
    if any(tags.get(k) != v for k, v in OWNER_TAGS.items()) or tags.get('DeploymentStack') != config.stack:
        raise DeploymentError('Use a new artifact bucket or this stack\'s owned artifact bucket. Arbitrary existing buckets are not modified.')
    return True


def build_plan(config, aws):
    """Only read operations. Validate account/plan before inspecting resources."""
    config.validate()
    plan = require_free_identity(config, aws)
    db = existing_cluster(config, aws)
    exists = check_stack_ownership(config, aws)
    digest = hashlib.sha256(config.stack.encode()).hexdigest()[:10]
    bucket = config.artifact_bucket or f'clx-{config.account}-{config.region}-{digest}'
    bucket_exists = check_bucket(config, aws, bucket)
    domain = config.domain_prefix or f'{config.prefix}-{config.account}-{digest[:6]}'
    for filename in ('express.yaml', 'operations.yaml'):
        aws.call('cloudformation', 'validate-template', '--template-body',
                 'file://' + str(ROOT / 'infra' / filename))
    return {
        'mode': 'read-only plan', 'account': config.account, 'region': config.region,
        'stack': config.stack, 'prefix': config.prefix, 'stack_exists': exists,
        'artifact_bucket': bucket, 'artifact_bucket_exists': bucket_exists,
        'cognito_domain_prefix': domain, 'existing_database': config.cluster,
        'aurora_host': db['Endpoint'], 'aurora_resource_id': db['DbClusterResourceId'],
        'free_plan_credits_usd': plan['accountPlanRemainingCredits']['amount'],
        'daily_recovery_enabled': config.daily_recovery,
        'resource_summary': 'New private S3/CloudFront, Cognito, HTTP API, 2 API Lambdas, private bootstrap, DynamoDB, SQS/DLQ and maintenance Lambda.',
        'database_scope': 'References the existing Express cluster. Creates the application schema only; does not create, modify or delete a DB cluster.',
        'cost_note': 'Free plan credits are consumed by usage. Daily recovery may briefly wake an idle database. No paid-plan upgrade is performed.',
    }


def validate_runtime(stage):
    """Catch the two prior deployment failures before uploading a broken ZIP."""
    if not (stage / 'typing_extensions.py').is_file():
        raise DeploymentError('Runtime package is missing typing_extensions.py.')
    if not (stage / 'psycopg' / '__init__.py').is_file():
        raise DeploymentError('Runtime package is missing psycopg.')
    modules = list((stage / 'psycopg_binary').glob('*.cpython-312-aarch64-*.so'))
    if not modules:
        raise DeploymentError('Runtime package needs a Linux aarch64 CPython 3.12 psycopg binary wheel.')
    native = list(stage.rglob('*.so')) + list(stage.rglob('*.so.*'))
    for path in native:
        header = path.read_bytes()[:20]
        if (len(header) < 20 or header[:6] != b'\x7fELF\x02\x01'
                or int.from_bytes(header[18:20], 'little') != 183):
            raise DeploymentError(f'Runtime library is not Linux ARM64: {path.name}.')


def create_archive(stage, ledger, destination):
    """Stable contents/timestamps; ledger is at ZIP root, not backend/ledger."""
    files = {}
    for source, prefix in ((stage, ''), (ledger, 'ledger/')):
        for path in source.rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo'):
                name = prefix + path.relative_to(source).as_posix()
                if name in files:
                    raise DeploymentError('Duplicate runtime archive entry: ' + name)
                files[name] = path
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in sorted(files.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def build_runtime(build):
    build.mkdir(parents=True, exist_ok=True)
    # A fresh directory avoids stale architecture-specific files; never recursively
    # delete a computed user path as part of deploying an application.
    stage = Path(tempfile.mkdtemp(prefix='express-runtime-', dir=build))
    command = [sys.executable, '-m', 'pip', '--isolated', 'install', '--index-url', 'https://pypi.org/simple',
               '--disable-pip-version-check', '--no-compile', '--no-deps', '--only-binary=:all:',
               # Python 3.12 Lambda uses Amazon Linux 2023 (glibc 2.34).
               # Current psycopg ARM wheels target glibc 2.27/2.28, not 2014.
               '--platform', 'manylinux_2_28_aarch64', '--platform', 'manylinux_2_27_aarch64',
               '--platform', 'manylinux2014_aarch64', '--implementation', 'cp',
               '--python-version', '3.12', '--abi', 'cp312', '--target', str(stage), *RUNTIME_REQUIREMENTS]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise DeploymentError('Linux ARM64 wheel download failed. Verify the pinned public PyPI packages and network access.')
    validate_runtime(stage)
    destination = stage.parent / (stage.name + '.zip')
    digest = create_archive(stage, ROOT / 'backend' / 'ledger', destination)
    return destination, digest


def ensure_bucket(config, aws, plan):
    bucket = plan['artifact_bucket']
    if plan['artifact_bucket_exists']:
        # Recheck at the point of use rather than trusting a stale plan.
        check_bucket(config, aws, bucket)
        return
    args = ['s3api', 'create-bucket', '--bucket', bucket, '--object-ownership', 'BucketOwnerEnforced']
    if config.region != 'us-east-1':
        args += ['--create-bucket-configuration', json.dumps({'LocationConstraint': config.region})]
    aws.call(*args)
    aws.call('s3api', 'put-public-access-block', '--bucket', bucket,
             '--expected-bucket-owner', config.account, '--public-access-block-configuration', json.dumps(PUBLIC_BLOCK))
    aws.call('s3api', 'put-bucket-encryption', '--bucket', bucket, '--expected-bucket-owner', config.account,
             '--server-side-encryption-configuration', json.dumps({'Rules': [
                 {'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]}))
    tags = dict(OWNER_TAGS, DeploymentStack=config.stack)
    aws.call('s3api', 'put-bucket-tagging', '--bucket', bucket, '--expected-bucket-owner', config.account,
             '--tagging', json.dumps({'TagSet': [{'Key': k, 'Value': v} for k, v in tags.items()]}))


def upload(aws, config, bucket, key, path, content_type):
    aws.call('s3api', 'put-object', '--bucket', bucket, '--key', key, '--body', str(path),
             '--expected-bucket-owner', config.account, '--content-type', content_type,
             '--cache-control', 'no-cache', '--server-side-encryption', 'AES256')


def apply(config, aws, plan):
    """Called only after an explicit --apply. No writes precede refreshed guards."""
    build = ROOT / '.build'
    artifact, digest = build_runtime(build)
    # Packaging can take time. Refresh every guard before the first cloud write.
    fresh = build_plan(config, aws)
    for key in ('account', 'region', 'stack', 'prefix', 'aurora_host', 'aurora_resource_id', 'artifact_bucket'):
        if fresh[key] != plan[key]:
            raise DeploymentError('Deployment inputs changed while packaging. Run a new read-only plan.')
    ensure_bucket(config, aws, fresh)
    bucket = fresh['artifact_bucket']
    runtime_key = f'{config.stack}/runtime/{digest}.zip'
    upload(aws, config, bucket, runtime_key, artifact, 'application/zip')
    operations = ROOT / 'infra' / 'operations.yaml'
    operations_hash = hashlib.sha256(operations.read_bytes()).hexdigest()
    operations_key = f'{config.stack}/templates/operations-{operations_hash}.yaml'
    upload(aws, config, bucket, operations_key, operations, 'application/yaml')
    parameters = {
        'ResourcePrefix': config.prefix, 'AuroraHost': fresh['aurora_host'],
        'AuroraClusterResourceId': fresh['aurora_resource_id'], 'AuroraUsername': config.username,
        'AuroraDatabase': config.database, 'LambdaArtifactBucket': bucket,
        'LambdaArtifactKey': runtime_key,
        'OperationsTemplateUrl': f'https://{bucket}.s3.{config.region}.amazonaws.com/{operations_key}',
        'CognitoDomainPrefix': fresh['cognito_domain_prefix'], 'DevelopmentOrigin': config.development_origin,
        'EnableDailyRecovery': str(config.daily_recovery).lower(),
    }
    aws.call('cloudformation', 'deploy', '--template-file', str(ROOT / 'infra' / 'express.yaml'),
             '--stack-name', config.stack, '--capabilities', 'CAPABILITY_NAMED_IAM',
             '--no-fail-on-empty-changeset', '--parameter-overrides',
             *(f'{k}={v}' for k, v in parameters.items()), '--tags',
             *(f'{k}={v}' for k, v in OWNER_TAGS.items()))
    stack = aws.call('cloudformation', 'describe-stacks', '--stack-name', config.stack)['Stacks'][0]
    outputs = {o['OutputKey']: o['OutputValue'] for o in stack['Outputs']}
    (build / 'express-outputs.json').write_text(json.dumps(outputs, indent=2) + '\n', encoding='utf-8')
    response_file = build / 'express-bootstrap-response.json'
    for attempt in range(3):
        result = aws.call('lambda', 'invoke', '--function-name', outputs['BootstrapFunctionName'],
                          '--payload', '{}', '--cli-binary-format', 'raw-in-base64-out', str(response_file))
        if not result.get('FunctionError'):
            break
        if attempt == 2:
            raise DeploymentError('Private schema bootstrap failed. Review its CloudWatch logs and .build/express-bootstrap-response.json. The stack remains available for retry.')
        time.sleep(10)
    browser = {
        'apiBaseUrl': outputs['ApiBaseUrl'], 'authMode': 'cognito',
        'cognitoDomain': outputs['CognitoDomain'], 'cognitoClientId': outputs['CognitoClientId'],
        'redirectUri': outputs['WebsiteUrl'], 'logoutUri': outputs['WebsiteUrl'],
        'oauthScopes': ['openid', 'email', 'ledger-api/access'],
    }
    generated = build / 'express-config.js'
    generated.write_text('window.APP_CONFIG = Object.freeze(' + json.dumps(browser, indent=2) + ');\n', encoding='utf-8')
    # Do not publish screenshots, local config files, source maps or frontend docs.
    for name, mime in (('index.html', 'text/html'), ('styles.css', 'text/css'), ('app.js', 'application/javascript')):
        upload(aws, config, outputs['FrontendBucketName'], name, ROOT / 'frontend' / name, mime)
    upload(aws, config, outputs['FrontendBucketName'], 'config.js', generated, 'application/javascript')
    aws.call('cloudfront', 'create-invalidation', '--distribution-id', outputs['DistributionId'], '--paths', '/*')
    return {'website': outputs['WebsiteUrl'], 'api_health': outputs['ApiBaseUrl'] + '/api/health',
            'outputs_file': str(build / 'express-outputs.json')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-account', required=True, help='12-digit intended AWS account ID')
    parser.add_argument('--region', required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--cluster-identifier', required=True, help='Existing Express Aurora cluster; never created by this script')
    parser.add_argument('--stack-name', default='capital-ledger-express')
    parser.add_argument('--resource-prefix', default='capital-ledger-express')
    parser.add_argument('--artifact-bucket', default='')
    parser.add_argument('--cognito-domain-prefix', default='')
    parser.add_argument('--database-user', default='postgres')
    parser.add_argument('--database-name', default='postgres')
    parser.add_argument('--development-origin', default='')
    parser.add_argument('--disable-daily-recovery', action='store_true')
    parser.add_argument('--apply', action='store_true', help='Explicitly create/update this new stack after all guards pass')
    args = parser.parse_args(argv)
    config = Config(account=args.expected_account, region=args.region, profile=args.profile,
                    cluster=args.cluster_identifier, stack=args.stack_name, prefix=args.resource_prefix,
                    artifact_bucket=args.artifact_bucket, domain_prefix=args.cognito_domain_prefix,
                    username=args.database_user, database=args.database_name,
                    development_origin=args.development_origin, daily_recovery=not args.disable_daily_recovery)
    try:
        config.validate()
        aws = Aws(config.profile, config.region)
        plan = build_plan(config, aws)
        print(json.dumps(plan, indent=2))
        if args.apply:
            print(json.dumps(apply(config, aws, plan), indent=2))
        else:
            print('Read-only plan complete. Review it, then rerun with --apply to create/update resources.')
        return 0
    except (DeploymentError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
