# Sign in to your personal AWS account

Use AWS CLI version 2.32.0 or newer for browser-based sign-in. On Windows, run the [official AWS CLI installer](https://awscli.amazonaws.com/AWSCLIV2.msi) to update an older installation, then reopen PowerShell.

```powershell
aws --version
aws login --profile ledger-dev --region us-east-2
```

Complete sign-in in your browser. The profile uses temporary credentials; access keys do not belong in this repository, the frontend, or a chat message. An IAM user or role needs the `SignInLocalDevelopmentAccess` managed policy for this login method. IAM Identity Center accounts instead use their organization's SSO setup.

Verify the profile:

```powershell
aws sts get-caller-identity --profile ledger-dev --region us-east-2
```

This identifies the signed-in account and does not create project resources. Signing in does not itself grant deployment permissions. The identity must also be authorized to create the resources listed in [deployment.md](deployment.md).

Choose the connection/deployment path in [deployment.md](deployment.md). The demonstrated deployment uses Aurora Express with IAM PostgreSQL authentication. The optional full-configuration SAM script is a different path and stops on Free accounts before creating resources. Authentication alone does not establish service availability or deployment permissions in your chosen region.

Reference: [AWS console-credential sign-in for local development](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html).
