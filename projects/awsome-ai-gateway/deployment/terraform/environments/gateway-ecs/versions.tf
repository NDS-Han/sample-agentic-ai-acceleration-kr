# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

terraform {
  required_version = ">= 1.9.0"
  required_providers {
    awscc = {
      source  = "hashicorp/awscc"
      version = ">= 1.25.0, < 2.0.0"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.12"
    }
  }
}
