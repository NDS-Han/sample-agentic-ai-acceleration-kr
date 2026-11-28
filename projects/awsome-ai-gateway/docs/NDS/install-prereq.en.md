# Prepare a deployment host

[한국어](install-prereq.md) · **English** · [NDS documentation](README.en.md)

Use an EC2 host with network access to AWS and the repository. Prepare the correct AWS account and region before you provision a gateway.

## Prepare the IAM role and instance

1. Create or select the organization's deployment IAM role and EC2 instance profile. The profile needs permission to provision the resources used by the chosen backend. Include Systems Manager access if the host uses Session Manager.
2. Launch an x86_64 Ubuntu LTS host in the deployment region. The tested build profile uses at least a t3.xlarge-class instance and 128 GB storage for image builds and caches. Restrict SSH to operator egress ranges when using SSH.
3. Attach the deployer profile. Once connected, confirm the caller and region with the AWS CLI after bootstrap.

## Install tools

Clone the organization’s NDS repository and run the tool installer from `projects/awsome-ai-gateway`. It prepares the AWS CLI, Docker, Kubernetes tools, and Terraform. Follow the script's session restart instructions so Docker group membership takes effect.

```bash
bash deployment/scripts/bootstrap-ec2.sh
aws sts get-caller-identity
docker info
terraform version
kubectl version --client
helm version
```

Confirm access to the intended Bedrock inference profiles or models in the account that will serve requests. Follow that account’s access and Marketplace procedure when required. Proceed to [EKS](install-eks.en.md), [ECS](install-ecs.en.md), or [Compose](install-compose.en.md). The [Korean host reference](install-prereq.md) includes exact instance-profile and bootstrap examples.
