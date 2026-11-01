"""Deployment boundaries: no AWS writes in a plan, reject wrong targets, package the real Lambda ABI."""
import contextlib
from dataclasses import replace
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('capital_ledger_express_deploy', ROOT / 'scripts' / 'deploy-express.py')
deploy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = deploy
SPEC.loader.exec_module(deploy)


class CloudFormationLoader(yaml.SafeLoader):
    pass


def intrinsic(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {suffix: value}


CloudFormationLoader.add_multi_constructor('!', intrinsic)


class ReadOnlyAWS:
    """Reject unanticipated operations instead of returning permissive defaults."""
    def __init__(self):
        self.calls = []
        self.identity = {'Account': '123456789012'}
        self.plan = {'accountId': '123456789012', 'accountPlanType': 'FREE',
                     'accountPlanStatus': 'ACTIVE',
                     'accountPlanRemainingCredits': {'amount': 100, 'unit': 'USD'}}
        self.db = {'DBClusterIdentifier': 'example-db',
                   'DBClusterArn': 'arn:aws:rds:us-east-2:123456789012:cluster:example-db',
                   'Engine': 'aurora-postgresql', 'Status': 'available', 'Port': 5432,
                   'IAMDatabaseAuthenticationEnabled': True, 'InternetAccessGatewayEnabled': True,
                   'ServerlessV2ScalingConfiguration': {'MinCapacity': 0},
                   'DbClusterResourceId': 'cluster-EXAMPLE123',
                   'Endpoint': 'example-db.cluster-abc.us-east-2.rds.amazonaws.com'}
        self.stack = None
        self.collision = None
        self.bucket = None
        self.queue_collision = False

    def call(self, *args):
        self.calls.append(args)
        pair = args[:2]
        if pair == ('sts', 'get-caller-identity'):
            return self.identity
        if pair == ('freetier', 'get-account-plan-state'):
            return self.plan
        if pair == ('rds', 'describe-db-clusters'):
            return {'DBClusters': [self.db]}
        if pair == ('cloudformation', 'describe-stacks'):
            if self.stack is None:
                raise deploy.AwsError(*pair, 'ValidationError')
            return {'Stacks': [self.stack]}
        if pair == ('lambda', 'get-function-configuration'):
            if self.collision:
                return {'FunctionName': self.collision}
            raise deploy.AwsError(*pair, 'ResourceNotFoundException')
        if pair == ('dynamodb', 'describe-table'):
            raise deploy.AwsError(*pair, 'ResourceNotFoundException')
        if pair == ('iam', 'get-role'):
            raise deploy.AwsError(*pair, 'NoSuchEntity')
        if pair == ('sqs', 'get-queue-url'):
            if self.queue_collision:
                return {'QueueUrl': 'https://sqs.us-east-2.amazonaws.com/123456789012/example'}
            raise deploy.AwsError(*pair, 'AWS.SimpleQueueService.NonExistentQueue')
        if pair == ('logs', 'describe-log-groups'):
            return {'logGroups': []}
        if pair == ('s3api', 'head-bucket'):
            if self.bucket is None:
                raise deploy.AwsError(*pair, '404')
            return {}
        if pair == ('s3api', 'get-bucket-location'):
            return {'LocationConstraint': 'us-east-2'}
        if pair == ('s3api', 'get-public-access-block'):
            return {'PublicAccessBlockConfiguration': deploy.PUBLIC_BLOCK}
        if pair == ('s3api', 'get-bucket-tagging'):
            return {'TagSet': self.bucket}
        if pair == ('cloudformation', 'validate-template'):
            return {}
        raise AssertionError('Unexpected operation, including any AWS mutation: ' + repr(pair))


class ExpressGuardTests(unittest.TestCase):
    def setUp(self):
        self.config = deploy.Config(account='123456789012', region='us-east-2',
                                    profile='test-only', cluster='example-db')
        self.aws = ReadOnlyAWS()

    def test_default_plan_never_calls_a_mutating_aws_operation(self):
        result = deploy.build_plan(self.config, self.aws)
        self.assertEqual(result['mode'], 'read-only plan')
        self.assertEqual(result['aurora_host'], self.aws.db['Endpoint'])
        self.assertFalse(result['stack_exists'])
        self.assertFalse(result['artifact_bucket_exists'])
        self.assertEqual(sum(c[:2] == ('cloudformation', 'validate-template') for c in self.aws.calls), 2)

    def test_wrong_account_stops_before_plan_and_resource_lookups(self):
        self.aws.identity['Account'] = '999999999999'
        with self.assertRaisesRegex(deploy.DeploymentError, 'does not match'):
            deploy.build_plan(self.config, self.aws)
        self.assertEqual([c[:2] for c in self.aws.calls], [('sts', 'get-caller-identity')])

    def test_unknown_paid_expired_or_empty_credit_plan_stops_before_resource_lookups(self):
        cases = ({'accountPlanType': 'PAID'}, {'accountPlanStatus': 'EXPIRED'},
                 {'accountPlanStatus': 'NOT_STARTED'}, {'accountId': '999999999999'},
                 {'accountPlanRemainingCredits': {'amount': 0, 'unit': 'USD'}},
                 {'accountPlanRemainingCredits': {}}, {'accountPlanRemainingCredits': {'amount': float('nan'), 'unit': 'USD'}})
        for change in cases:
            with self.subTest(change=change):
                aws = ReadOnlyAWS()
                aws.plan.update(change)
                with self.assertRaises(deploy.DeploymentError):
                    deploy.build_plan(self.config, aws)
                self.assertEqual([c[0] for c in aws.calls], ['sts', 'freetier'])

    def test_only_matching_iam_express_autopause_cluster_can_be_used(self):
        cases = ({'DBClusterArn': 'arn:aws:rds:us-west-2:123456789012:cluster:example-db'},
                 {'Engine': 'postgres'}, {'IAMDatabaseAuthenticationEnabled': False},
                 {'InternetAccessGatewayEnabled': False}, {'Port': 6543},
                 {'ServerlessV2ScalingConfiguration': {'MinCapacity': 0.5}},
                 {'DbClusterResourceId': ''}, {'Endpoint': 'wrong.cluster.example.us-west-2.rds.amazonaws.com'})
        for change in cases:
            with self.subTest(change=change):
                aws = ReadOnlyAWS()
                aws.db.update(change)
                with self.assertRaises(deploy.DeploymentError):
                    deploy.build_plan(self.config, aws)
                self.assertEqual([c[0] for c in aws.calls], ['sts', 'freetier', 'rds'])

    def test_manual_prefix_and_existing_function_are_never_adopted(self):
        with self.assertRaisesRegex(deploy.DeploymentError, 'reserved'):
            deploy.build_plan(replace(self.config, prefix='capital-ledger'), self.aws)
        self.assertEqual(self.aws.calls, [])
        self.aws.collision = 'capital-ledger-express-customers'
        with self.assertRaisesRegex(deploy.DeploymentError, 'already in use'):
            deploy.build_plan(self.config, self.aws)

    def test_foreign_stack_rejected_but_owned_stack_can_be_updated(self):
        self.aws.stack = {'StackStatus': 'CREATE_COMPLETE', 'Tags': [], 'Parameters': []}
        with self.assertRaisesRegex(deploy.DeploymentError, 'not owned'):
            deploy.build_plan(self.config, self.aws)
        self.aws.stack.update(Tags=[{'Key': k, 'Value': v} for k, v in deploy.OWNER_TAGS.items()],
                              Parameters=[{'ParameterKey': 'ResourcePrefix', 'ParameterValue': self.config.prefix}])
        self.assertTrue(deploy.build_plan(self.config, self.aws)['stack_exists'])

    def test_existing_unowned_queue_cannot_be_adopted_by_createqueue_idempotency(self):
        self.aws.queue_collision = True
        with self.assertRaisesRegex(deploy.DeploymentError, 'queue already exists outside'):
            deploy.build_plan(self.config, self.aws)

    def test_arbitrary_existing_artifact_bucket_is_rejected(self):
        self.aws.bucket = []
        with self.assertRaisesRegex(deploy.DeploymentError, 'owned artifact bucket'):
            deploy.build_plan(self.config, self.aws)

    def test_cli_default_does_not_build_or_apply(self):
        argv = ['--expected-account', self.config.account, '--region', self.config.region,
                '--profile', self.config.profile, '--cluster-identifier', self.config.cluster]
        with patch.object(deploy, 'Aws', return_value=self.aws), patch.object(deploy, 'apply') as apply:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(deploy.main(argv), 0)
            apply.assert_not_called()

    def test_identity_and_plan_are_refreshed_after_packaging_before_first_write(self):
        plan = deploy.build_plan(self.config, self.aws)
        self.aws.plan['accountPlanStatus'] = 'EXPIRED'
        self.aws.calls.clear()
        with patch.object(deploy, 'build_runtime', return_value=(Path('unused.zip'), 'digest')):
            with self.assertRaises(deploy.DeploymentError):
                deploy.apply(self.config, self.aws, plan)
        self.assertEqual([c[0] for c in self.aws.calls], ['sts', 'freetier'])

    def test_cli_deploy_progress_is_not_misparsed_as_json(self):
        aws = object.__new__(deploy.Aws)
        aws.executable, aws.base = 'aws', []
        completed = Mock(returncode=0, stdout='Waiting for changeset to be created..\nSuccessfully created stack\n', stderr='')
        with patch.object(deploy.subprocess, 'run', return_value=completed):
            self.assertEqual(aws.call('cloudformation', 'deploy', '--stack-name', 'example'), {})


class RuntimeArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_root = ROOT / '.build' / 'test-express'
        cls.temp_root.mkdir(parents=True, exist_ok=True)

    def make_runtime(self, root):
        stage, ledger = root / 'site', root / 'ledger'
        (stage / 'psycopg_binary').mkdir(parents=True)
        (stage / 'psycopg').mkdir()
        ledger.mkdir()
        (stage / 'typing_extensions.py').write_text('# pinned typing support\n')
        (stage / 'psycopg' / '__init__.py').write_text('# driver\n')
        binary = stage / 'psycopg_binary' / 'pq.cpython-312-aarch64-linux-gnu.so'
        header = bytearray(20)
        header[:6] = b'\x7fELF\x02\x01'
        header[18:20] = (183).to_bytes(2, 'little')
        binary.write_bytes(header)
        (ledger / '__init__.py').write_text('')
        (ledger / 'handlers.py').write_text('# application\n')
        (ledger / 'aws-trust-ca.pem').write_text('test fixture certificate\n')
        return stage, ledger, binary

    def test_archive_is_reproducible_and_has_correct_root_and_trust_bundle(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as tmp:
            root = Path(tmp)
            stage, ledger, _ = self.make_runtime(root)
            deploy.validate_runtime(stage)
            a, b = root / 'one.zip', root / 'two.zip'
            self.assertEqual(deploy.create_archive(stage, ledger, a), deploy.create_archive(stage, ledger, b))
            with zipfile.ZipFile(a) as archive:
                self.assertIn('typing_extensions.py', archive.namelist())
                self.assertIn('ledger/handlers.py', archive.namelist())
                self.assertIn('ledger/aws-trust-ca.pem', archive.namelist())
                self.assertFalse(any(n.startswith('backend/') for n in archive.namelist()))

    def test_wrong_native_architecture_or_missing_typing_dependency_fails_locally(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as tmp:
            stage, _, binary = self.make_runtime(Path(tmp))
            data = bytearray(binary.read_bytes())
            data[18:20] = (62).to_bytes(2, 'little')  # x86_64
            binary.write_bytes(data)
            with self.assertRaisesRegex(deploy.DeploymentError, 'not Linux ARM64'):
                deploy.validate_runtime(stage)
            (stage / 'typing_extensions.py').unlink()
            with self.assertRaisesRegex(deploy.DeploymentError, 'typing_extensions'):
                deploy.validate_runtime(stage)

    def test_wrong_python_abi_is_rejected_even_when_machine_is_arm64(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as tmp:
            stage, _, binary = self.make_runtime(Path(tmp))
            binary.rename(binary.with_name('pq.cpython-313-aarch64-linux-gnu.so'))
            with self.assertRaisesRegex(deploy.DeploymentError, 'CPython 3.12'):
                deploy.validate_runtime(stage)


class ExpressApplyContractTests(unittest.TestCase):
    def test_apply_publishes_only_four_frontend_assets_after_private_bootstrap(self):
        # Exercise command construction without AWS. This catches accidental
        # publishing of local configs/screenshots/docs from frontend/.
        test_root = ROOT / '.build' / 'test-express'
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_root) as tmp:
            root = Path(tmp)
            (root / 'infra').mkdir()
            (root / 'infra' / 'operations.yaml').write_text('AWSTemplateFormatVersion: 2010-09-09\n')
            (root / 'frontend').mkdir()
            for name in ('index.html', 'styles.css', 'app.js', 'config.local.js', 'preview.jpg', 'FRONTEND.md'):
                (root / 'frontend' / name).write_text('fixture\n')
            build = root / '.build'
            build.mkdir()
            artifact = build / 'runtime.zip'
            artifact.write_bytes(b'fixture archive')
            config = deploy.Config(account='123456789012', region='us-east-2', profile='test-only', cluster='example-db')
            plan = deploy.build_plan(config, ReadOnlyAWS())
            outputs = {
                'BootstrapFunctionName': 'test-bootstrap', 'WebsiteUrl': 'https://test.cloudfront.net/',
                'ApiBaseUrl': 'https://api.example.test', 'CognitoDomain': 'https://login.example.test',
                'CognitoClientId': 'public-client', 'FrontendBucketName': 'frontend-fixture', 'DistributionId': 'TESTID',
            }
            calls = []

            def aws_call(*args):
                calls.append(args)
                if args[:2] == ('cloudformation', 'describe-stacks'):
                    return {'Stacks': [{'Outputs': [{'OutputKey': k, 'OutputValue': v} for k, v in outputs.items()]}]}
                if args[:2] == ('lambda', 'invoke'):
                    return {'StatusCode': 200}
                return {}

            aws = Mock(call=aws_call)
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'build_plan', return_value=plan) as refreshed:
                with patch.object(deploy, 'build_runtime', return_value=(artifact, 'fixed-artifact-hash')):
                    result = deploy.apply(config, aws, plan)
            refreshed.assert_called_once_with(config, aws)
            self.assertEqual(result['website'], outputs['WebsiteUrl'])
            frontend_puts = [c for c in calls if c[:2] == ('s3api', 'put-object') and c[c.index('--bucket') + 1] == 'frontend-fixture']
            self.assertEqual({c[c.index('--key') + 1] for c in frontend_puts}, {'index.html', 'styles.css', 'app.js', 'config.js'})
            bootstrap_index = next(i for i, c in enumerate(calls) if c[:2] == ('lambda', 'invoke'))
            self.assertTrue(all(calls.index(c) > bootstrap_index for c in frontend_puts))
            browser = json.loads((build / 'express-config.js').read_text().split('Object.freeze(', 1)[1][:-3])
            self.assertEqual(browser['redirectUri'], browser['logoutUri'])
            self.assertEqual(browser['oauthScopes'], ['openid', 'email', 'ledger-api/access'])
            self.assertNotIn('secret', json.dumps(browser).lower())


class ExpressTemplateBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = yaml.load((ROOT / 'infra' / 'express.yaml').read_text(), Loader=CloudFormationLoader)
        cls.resources = cls.template['Resources']

    def test_external_database_is_referenced_without_paid_vpc_or_secret_resources(self):
        forbidden = ('AWS::RDS::', 'AWS::EC2::', 'AWS::SecretsManager::', 'AWS::Cognito::IdentityPool')
        self.assertFalse([r['Type'] for r in self.resources.values() if r['Type'].startswith(forbidden)])
        operations = self.resources['Operations']['Properties']['Parameters']
        self.assertEqual(operations['AuroraClusterResourceId'], {'Ref': 'AuroraClusterResourceId'})
        for name in ('CustomersFunction', 'TransactionsFunction'):
            function = self.resources[name]['Properties']
            self.assertNotIn('VpcConfig', function)
            self.assertEqual(function['Environment']['Variables']['DB_BACKEND'], 'aurora_iam')
            self.assertEqual(function['Environment']['Variables']['OUTBOX_QUEUE_URL'], {'GetAtt': 'Operations.Outputs.OutboxQueueUrl'})

    def test_every_customer_route_requires_access_scope_and_bootstrap_is_private(self):
        routes = [r['Properties'] for r in self.resources.values() if r['Type'] == 'AWS::ApiGatewayV2::Route']
        self.assertEqual(len(routes), 9)
        for route in routes:
            if route['RouteKey'] == 'GET /api/health':
                self.assertEqual(route['AuthorizationType'], 'NONE')
            else:
                self.assertEqual(route['AuthorizationType'], 'JWT')
                self.assertEqual(route['AuthorizationScopes'], ['ledger-api/access'])
        for resource in self.resources.values():
            self.assertNotEqual(resource['Type'], 'AWS::Lambda::Url')
            if resource['Type'] == 'AWS::Lambda::Permission':
                self.assertNotEqual(resource['Properties']['FunctionName'], {'Ref': 'BootstrapFunction'})
        client = self.resources['UserPoolClient']['Properties']
        self.assertFalse(client['GenerateSecret'])
        self.assertEqual(client['AllowedOAuthFlows'], ['code'])
        self.assertEqual(client['CallbackURLs'][0], client['DefaultRedirectURI'])

    def test_website_requires_cloudfront_and_https(self):
        bucket = self.resources['FrontendBucket']['Properties']
        self.assertEqual(bucket['PublicAccessBlockConfiguration'], deploy.PUBLIC_BLOCK)
        statement = self.resources['FrontendBucketPolicy']['Properties']['PolicyDocument']['Statement'][0]
        self.assertEqual(statement['Principal'], {'Service': 'cloudfront.amazonaws.com'})
        self.assertIn('AWS:SourceArn', statement['Condition']['StringEquals'])
        cache = self.resources['Distribution']['Properties']['DistributionConfig']['DefaultCacheBehavior']
        self.assertEqual(cache['ViewerProtocolPolicy'], 'redirect-to-https')
        self.assertNotIn('*', self.resources['HttpApi']['Properties']['CorsConfiguration']['AllowOrigins'])


if __name__ == '__main__':
    unittest.main()
