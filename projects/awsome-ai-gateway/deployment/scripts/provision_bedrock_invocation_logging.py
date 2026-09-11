#!/usr/bin/env python3
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Provision Bedrock **model invocation logging** for the standard runtime plane.

Why this exists: the gateway's own ``usage_logs`` rows record token counts and cost but
NOT the prompt/response bodies, so "what exactly did this user send the model?" has never
been answerable from our data. Bedrock's model-invocation logging answers it on the
service side, and ``usage_logs.bedrock_request_id`` (the AWS ``x-amzn-requestid``) is the
join key from a gateway request to its log record.

⚠️ Only the **standard runtime plane** is captured. Per the AWS docs:

    "Model invocation logging is only supported for calls made through the
     bedrock-runtime endpoint. This includes the OpenAI-compatible Responses and
     Chat Completions APIs on that endpoint. Calls made through other endpoints,
     such as the same APIs on bedrock-mantle, are not currently captured by
     invocation logging."
    — docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html

So Mantle traffic (provider ``BEDROCK_MANTLE`` / ``BEDROCK_MANTLE_OPENAI``) produces NO
record no matter what is configured here. That is not a gap to fix — it is *the* reason
the runtime plane exists alongside Mantle for GPT-5.6, and why the reconciler reports a
NULL ``bedrock_request_id`` on a Mantle row as ``skipped_null``/``mantle_plane`` rather
than as a missing log.

⚠️ Logging is **per-Region and account-wide within that Region** — there is no per-model
or per-principal switch. Region isolation is therefore the only scoping lever we have,
which is why ``--regions`` defaults to ``us-east-2`` alone (where the GPT-5.6 CRIS
profiles execute) and why ap-northeast-2 must stay unconfigured: enabling it there would
start capturing the *bodies* of every Claude call from every app in Seoul.

Idempotent (re-run safe): reuses an existing log group / role / bucket and overwrites the
logging configuration with the same value.

Subcommands:
    deploy    (default) create/reuse the CloudWatch log group, the S3 large-body sidecar
              bucket, and the delivery IAM role, then PutModelInvocationLoggingConfiguration
              for each --regions entry.
    status    print the live configuration for every region in --regions (plus Seoul, as a
              standing negative control) and whether each backing resource exists.
    teardown  delete the logging configuration (and the role, which is then useless).
              Log group and bucket are KEPT unless you pass --delete-log-group /
              --delete-bucket, because they hold audit data.

Env overrides: PROJECT (llm-gateway), ENVIRONMENT (dev), LOG_GROUP_NAME.

Examples:
    # dev, us-east-2 only (the default) — show what would happen first
    python3 deployment/scripts/provision_bedrock_invocation_logging.py deploy --dry-run
    python3 deployment/scripts/provision_bedrock_invocation_logging.py deploy
    python3 deployment/scripts/provision_bedrock_invocation_logging.py status
    # stop collecting bodies but keep everything already collected
    python3 deployment/scripts/provision_bedrock_invocation_logging.py teardown
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

PROJECT = os.environ.get("PROJECT", "llm-gateway")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")

# Bedrock writes every record to this ONE log stream, whose name is fixed by the service
# (see the role-policy example in the docs). The delivery role's `logs:PutLogEvents` is
# scoped to exactly this stream — widening it to `:*` would let the role write anywhere in
# the group, and narrowing it wrongly is a silent failure (the config saves fine and no
# record ever arrives, because delivery errors are not surfaced to us).
LOG_STREAM_NAME = "aws/bedrock/modelinvocations"
DEFAULT_LOG_GROUP = os.environ.get("LOG_GROUP_NAME", "/aws/bedrock/modelinvocations")

# Regions where the GPT-5.6 CRIS profiles actually execute. `us.openai.gpt-5.6-*` fans out
# to us-east-1/us-east-2/us-west-2 and `global.` resolves to us-east-2 (measured
# 2026-09-03 via `aws bedrock get-inference-profile`), so us-east-2 is the one Region that
# both profile families land in — enabling it there captures our GPT-5.6 traffic without
# touching any other Region.
DEFAULT_REGIONS = ["us-east-2"]

# Never enable logging here without an explicit decision: Seoul is where every Claude call
# from every app runs, so turning it on captures those bodies account-wide. `status` asserts
# this Region stays unconfigured so an accidental console click gets caught.
NEGATIVE_CONTROL_REGION = "ap-northeast-2"

LARGE_DATA_PREFIX = "large-data"


def log(msg: str) -> None:
    print(f"[invlog] {msg}", file=sys.stderr)


def fail(msg: str) -> None:
    print(f"[invlog] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def account_id() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


def role_name(region: str) -> str:
    """Per-Region delivery role.

    One role per Region rather than one shared role, because the role's inline policy names
    the Region's log-group ARN. A single shared role would have to accumulate every Region's
    ARNs, and then tearing down one Region would rewrite the policy and silently break
    delivery in the others.
    """
    return f"{PROJECT}-{ENVIRONMENT}-bedrock-invlog-{region}"


def bucket_name(acct: str, region: str) -> str:
    """Per-Region sidecar bucket.

    S3 bucket names are global but buckets are regional, and the docs require the log
    bucket to be in the same account AND Region as the logging configuration — so the
    Region has to be in the name or a second Region could never be enabled.
    """
    return f"{PROJECT}-{ENVIRONMENT}-bedrock-invlogs-{acct}-{region}"


def tags() -> list[dict]:
    return [
        {"Key": "Project", "Value": PROJECT},
        {"Key": "Environment", "Value": ENVIRONMENT},
        {"Key": "ManagedBy", "Value": "provision_bedrock_invocation_logging.py"},
    ]


# ── CloudWatch log group ──────────────────────────────────────────────────────
def ensure_log_group(region: str, name: str, retention_days: int, dry: bool) -> str:
    logs = boto3.client("logs", region_name=region)
    existing = logs.describe_log_groups(logGroupNamePrefix=name).get("logGroups", [])
    match = next((g for g in existing if g.get("logGroupName") == name), None)
    if match:
        log(f"{region}: log group reused: {name} (retention={match.get('retentionInDays')})")
        arn = match.get("arn")
        if not dry and match.get("retentionInDays") != retention_days:
            logs.put_retention_policy(logGroupName=name, retentionInDays=retention_days)
            log(f"{region}: retention set to {retention_days}d")
        return arn or f"arn:aws:logs:{region}:*:log-group:{name}"
    if dry:
        log(f"[dry-run] {region}: create log group {name} (retention={retention_days}d)")
        return f"arn:aws:logs:{region}:*:log-group:{name}"
    logs.create_log_group(logGroupName=name, tags={t["Key"]: t["Value"] for t in tags()})
    logs.put_retention_policy(logGroupName=name, retentionInDays=retention_days)
    log(f"{region}: log group created: {name} (retention={retention_days}d)")
    return f"arn:aws:logs:{region}:*:log-group:{name}"


# ── S3 sidecar bucket (bodies > 100 KB and binary data) ───────────────────────
def _bucket_exists(s3, bucket: str) -> bool:
    try:
        s3.head_bucket(Bucket=bucket)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchBucket", "403"):
            # 403 means it exists and belongs to someone else — surface that rather than
            # trying to create it and getting a confusing BucketAlreadyExists.
            if e.response["Error"]["Code"] == "403":
                fail(f"bucket {bucket} exists but is not accessible from this account")
            return False
        raise


def ensure_bucket(acct: str, region: str, kms_key_arn: str | None, lifecycle_days: int, dry: bool) -> str:
    s3 = boto3.client("s3", region_name=region)
    bucket = bucket_name(acct, region)

    if _bucket_exists(s3, bucket):
        log(f"{region}: sidecar bucket reused: {bucket}")
    elif dry:
        log(f"[dry-run] {region}: create sidecar bucket {bucket}")
        return bucket
    else:
        kwargs: dict = {"Bucket": bucket, "ObjectOwnership": "BucketOwnerEnforced"}
        # us-east-1 is the one Region that must NOT get a LocationConstraint.
        if region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**kwargs)
        log(f"{region}: sidecar bucket created: {bucket}")

    if dry:
        return bucket

    # ACLs disabled is a hard requirement, not hardening: "The bucket ACL must be disabled
    # in order for the bucket policy to take effect." Set it on reuse too, since a bucket
    # created by hand may have ACLs on.
    s3.put_bucket_ownership_controls(
        Bucket=bucket, OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    )
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,  # blocks *public* policies; a service-principal policy is fine
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})

    if kms_key_arn:
        # SSE-KMS with a customer-managed key needs a kms:GenerateDataKey grant for
        # bedrock.amazonaws.com IN THE KEY POLICY. This script deliberately does not edit
        # KMS key policies (a key is shared, security-critical, and easy to lock yourself
        # out of) — it prints the statement to add. Without it the config saves and every
        # large body is silently dropped.
        s3.put_bucket_encryption(
            Bucket=bucket,
            ServerSideEncryptionConfiguration={
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "aws:kms",
                            "KMSMasterKeyID": kms_key_arn,
                        },
                        "BucketKeyEnabled": True,
                    }
                ]
            },
        )
        log(f"{region}: SSE-KMS enabled with {kms_key_arn}")
        print(
            "[invlog] ADD THIS STATEMENT TO THE KMS KEY POLICY (not done automatically):\n"
            + json.dumps(
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "bedrock.amazonaws.com"},
                    "Action": "kms:GenerateDataKey",
                    "Resource": "*",
                    "Condition": {
                        "StringEquals": {"aws:SourceAccount": acct},
                        "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{region}:{acct}:*"},
                    },
                },
                indent=2,
            ),
            file=sys.stderr,
        )
    else:
        # Default to SSE-S3: it needs no key policy, so there is no way to end up with a
        # config that looks healthy while large bodies are being dropped.
        s3.put_bucket_encryption(
            Bucket=bucket,
            ServerSideEncryptionConfiguration={
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
                        "BucketKeyEnabled": True,
                    }
                ]
            },
        )
        log(f"{region}: SSE-S3 (AES256) enabled")

    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-invocation-log-bodies",
                    "Status": "Enabled",
                    "Filter": {"Prefix": f"{LARGE_DATA_PREFIX}/"},
                    "Expiration": {"Days": lifecycle_days},
                    # Versioning is on, so an Expiration alone only adds delete markers.
                    # Without this the "expired" prompt bodies live forever as noncurrent
                    # versions — i.e. the retention promise would be cosmetic.
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
                },
                {
                    "ID": "abort-incomplete-multipart",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
                },
            ]
        },
    )
    log(f"{region}: lifecycle set ({lifecycle_days}d on {LARGE_DATA_PREFIX}/)")

    s3.put_bucket_policy(
        Bucket=bucket,
        Policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "AmazonBedrockLogsWrite",
                        "Effect": "Allow",
                        "Principal": {"Service": "bedrock.amazonaws.com"},
                        "Action": "s3:PutObject",
                        # Scoped to the bucket, not to a key path. The docs pin the exact
                        # layout only for the `s3Config` destination
                        # (`{prefix}/AWSLogs/{acct}/BedrockModelInvocationLogs/*`); for
                        # `largeDataDeliveryS3Config` they say only "under the data
                        # prefix". Guessing that path wrong drops every large body with no
                        # error anywhere, and this bucket is dedicated to Bedrock, so the
                        # real boundary is the two conditions below — they are what stop
                        # any other account or Region from writing here.
                        "Resource": f"arn:aws:s3:::{bucket}/*",
                        "Condition": {
                            "StringEquals": {"aws:SourceAccount": acct},
                            "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{region}:{acct}:*"},
                        },
                    }
                ],
            }
        ),
    )
    log(f"{region}: bucket policy attached (bedrock.amazonaws.com, SourceAccount={acct})")
    return bucket


# ── Delivery IAM role ─────────────────────────────────────────────────────────
def ensure_role(acct: str, region: str, log_group: str, bucket: str, dry: bool) -> str:
    name = role_name(region)
    arn = f"arn:aws:iam::{acct}:role/{name}"
    if dry:
        log(f"[dry-run] {region}: IAM delivery role {name}")
        return arn

    # The two conditions are the confused-deputy guard from the docs: without them any
    # account's Bedrock could assume this role.
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowBedrockLogDelivery",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": acct},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{region}:{acct}:*"},
                },
            }
        ],
    }
    perms = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "WriteInvocationLogs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": (
                    f"arn:aws:logs:{region}:{acct}:log-group:{log_group}"
                    f":log-stream:{LOG_STREAM_NAME}"
                ),
            },
            {
                # Whether large-body delivery uses this role or the bucket policy's service
                # principal is not documented, so grant both paths. Granting only one and
                # guessing wrong loses exactly the >100 KB prompts — the ones most worth
                # auditing — with no error surfaced.
                "Sid": "WriteLargeBodies",
                "Effect": "Allow",
                "Action": "s3:PutObject",
                "Resource": f"arn:aws:s3:::{bucket}/*",
            },
        ],
    }
    iam = boto3.client("iam")
    try:
        iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description=f"Bedrock model-invocation-log delivery ({region})",
            Tags=tags(),
        )
        log(f"{region}: IAM role created: {name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            # Re-put the trust policy: a hand-edited or older-revision trust doc is exactly
            # the kind of drift that makes delivery fail silently.
            iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
            log(f"{region}: IAM role reused: {name} (trust policy refreshed)")
        else:
            raise
    iam.put_role_policy(
        RoleName=name, PolicyName="bedrock-invocation-log-delivery", PolicyDocument=json.dumps(perms)
    )
    return arn


# ── Logging configuration ─────────────────────────────────────────────────────
def put_logging_config(
    region: str,
    log_group: str,
    role_arn: str,
    bucket: str,
    modalities: set[str],
    dry: bool,
) -> None:
    cfg = {
        "cloudWatchConfig": {
            "logGroupName": log_group,
            "roleArn": role_arn,
            "largeDataDeliveryS3Config": {
                "bucketName": bucket,
                "keyPrefix": LARGE_DATA_PREFIX,
            },
        },
        "textDataDeliveryEnabled": "text" in modalities,
        "imageDataDeliveryEnabled": "image" in modalities,
        "embeddingDataDeliveryEnabled": "embedding" in modalities,
        "videoDataDeliveryEnabled": "video" in modalities,
    }
    if dry:
        log(f"[dry-run] {region}: PutModelInvocationLoggingConfiguration\n{json.dumps(cfg, indent=2)}")
        return

    bedrock = boto3.client("bedrock", region_name=region)
    # A freshly created role is not yet assumable by the service, and Bedrock validates
    # assumability inline — so the first call after create_role legitimately fails with
    # ValidationException. Retry rather than making the operator re-run and wonder.
    last_err: Exception | None = None
    for attempt in range(1, 13):
        try:
            bedrock.put_model_invocation_logging_configuration(loggingConfig=cfg)
            log(f"{region}: logging configuration applied")
            return
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code not in ("ValidationException", "AccessDeniedException"):
                raise
            last_err = e
            log(f"{region}: attempt {attempt}/12 not ready yet ({code}) — waiting 5s")
            time.sleep(5)
    fail(f"{region}: could not apply logging configuration after 12 attempts: {last_err}")


def get_logging_config(region: str) -> dict | None:
    """Live configuration, or None when logging is off in this Region.

    ``GetModelInvocationLoggingConfiguration`` returns HTTP 200 with **no**
    ``loggingConfig`` key when logging is disabled — it does not raise. Treating the empty
    response as an error would make `status` claim a misconfiguration, and treating a
    missing key as truthy would make the negative-control assertion useless.
    """
    try:
        resp = boto3.client("bedrock", region_name=region).get_model_invocation_logging_configuration()
    except ClientError as e:
        log(f"{region}: get config failed: {e.response['Error']['Code']}")
        return None
    return resp.get("loggingConfig") or None


# ── Subcommands ───────────────────────────────────────────────────────────────
def cmd_deploy(args) -> None:
    acct = account_id()
    modalities = {m.strip() for m in args.modalities.split(",") if m.strip()}
    unknown = modalities - {"text", "image", "embedding", "video"}
    if unknown:
        fail(f"unknown modalities: {sorted(unknown)} (allowed: text,image,embedding,video)")
    if not modalities:
        fail("--modalities must list at least one modality")

    if NEGATIVE_CONTROL_REGION in args.regions and not args.allow_seoul:
        fail(
            f"{NEGATIVE_CONTROL_REGION} captures the request bodies of EVERY Claude call from "
            "every app account-wide (logging has no per-model scope). Pass --allow-seoul only "
            "with an explicit decision to do that."
        )
    if ENVIRONMENT != "dev" and not args.allow_non_dev:
        fail(
            f"ENVIRONMENT={ENVIRONMENT!r}: enabling body capture outside dev stores real user "
            "prompts. Re-run with --allow-non-dev once that is signed off."
        )

    log(f"account={acct} project={PROJECT} env={ENVIRONMENT} regions={args.regions}")
    for region in args.regions:
        ensure_log_group(region, args.log_group, args.retention_days, args.dry_run)
        bucket = ensure_bucket(acct, region, args.kms_key_arn, args.lifecycle_days, args.dry_run)
        role_arn = ensure_role(acct, region, args.log_group, bucket, args.dry_run)
        put_logging_config(region, args.log_group, role_arn, bucket, modalities, args.dry_run)

    print(f"BEDROCK_INVOCATION_LOG_GROUP={args.log_group}")
    print(f"BEDROCK_INVOCATION_LOG_REGIONS={','.join(args.regions)}")
    print(f"BEDROCK_INVOCATION_LOG_BUCKET={bucket_name(acct, args.regions[0])}")
    if not args.dry_run:
        log("Set these under `aws.bedrockInvocationLogging` in the helm values, then redeploy.")
        log("Verify: python3 deployment/scripts/provision_bedrock_invocation_logging.py status")


def cmd_status(args) -> None:
    acct = account_id()
    regions = list(args.regions)
    if NEGATIVE_CONTROL_REGION not in regions:
        regions.append(NEGATIVE_CONTROL_REGION)

    problems = 0
    for region in regions:
        cfg = get_logging_config(region)
        expected_off = region == NEGATIVE_CONTROL_REGION and not args.allow_seoul
        if cfg is None:
            state = "OFF (expected)" if expected_off else "OFF"
            print(f"{region}: invocation logging {state}")
            if not expected_off:
                problems += 1
            continue
        if expected_off:
            print(f"{region}: ⚠️ invocation logging ON — this Region should be OFF")
            print(f"  {json.dumps(cfg)}")
            problems += 1
            continue
        cw = cfg.get("cloudWatchConfig", {}) or {}
        ldd = cw.get("largeDataDeliveryS3Config", {}) or {}
        print(f"{region}: invocation logging ON")
        print(f"  logGroup: {cw.get('logGroupName')}")
        print(f"  roleArn:  {cw.get('roleArn')}")
        print(f"  largeData: s3://{ldd.get('bucketName')}/{ldd.get('keyPrefix')}")
        print(f"  s3Config: {cfg.get('s3Config') or 'none (CloudWatch only)'}")
        print(
            "  modalities: "
            + ",".join(
                k.replace("DataDeliveryEnabled", "")
                for k in ("textDataDeliveryEnabled", "imageDataDeliveryEnabled",
                          "embeddingDataDeliveryEnabled", "videoDataDeliveryEnabled")
                if cfg.get(k)
            )
        )
        expected_role = f"arn:aws:iam::{acct}:role/{role_name(region)}"
        if cw.get("roleArn") != expected_role:
            print(f"  ⚠️ roleArn is not the one this script manages ({expected_role})")
            problems += 1
    if problems:
        log(f"{problems} region(s) not in the expected state")
        sys.exit(2)


def cmd_teardown(args) -> None:
    acct = account_id()
    for region in args.regions:
        bedrock = boto3.client("bedrock", region_name=region)
        if get_logging_config(region) is None:
            log(f"{region}: logging already OFF — skip")
        elif args.dry_run:
            log(f"[dry-run] {region}: DeleteModelInvocationLoggingConfiguration")
        else:
            bedrock.delete_model_invocation_logging_configuration()
            log(f"{region}: logging configuration deleted")

        name = role_name(region)
        if args.dry_run:
            log(f"[dry-run] {region}: delete IAM role {name}")
        else:
            iam = boto3.client("iam")
            try:
                iam.delete_role_policy(RoleName=name, PolicyName="bedrock-invocation-log-delivery")
                iam.delete_role(RoleName=name)
                log(f"{region}: IAM role deleted: {name}")
            except ClientError as e:
                if e.response["Error"]["Code"] == "NoSuchEntity":
                    log(f"{region}: IAM role NONE — skip")
                else:
                    raise

        # Log group and bucket hold the collected audit data, so they survive teardown by
        # default: turning collection off is reversible, deleting the evidence is not.
        if args.delete_log_group:
            if args.dry_run:
                log(f"[dry-run] {region}: DELETE log group {args.log_group} AND ITS RECORDS")
            else:
                try:
                    boto3.client("logs", region_name=region).delete_log_group(
                        logGroupName=args.log_group
                    )
                    log(f"{region}: log group deleted: {args.log_group}")
                except ClientError as e:
                    if e.response["Error"]["Code"] == "ResourceNotFoundException":
                        log(f"{region}: log group NONE — skip")
                    else:
                        raise
        else:
            log(f"{region}: log group kept ({args.log_group}) — pass --delete-log-group to remove")

        bucket = bucket_name(acct, region)
        if args.delete_bucket:
            if args.dry_run:
                log(f"[dry-run] {region}: EMPTY AND DELETE bucket {bucket}")
            else:
                _delete_bucket(region, bucket)
        else:
            log(f"{region}: sidecar bucket kept ({bucket}) — pass --delete-bucket to remove")
    log("teardown complete.")


def _delete_bucket(region: str, bucket: str) -> None:
    s3 = boto3.client("s3", region_name=region)
    if not _bucket_exists(s3, bucket):
        log(f"{region}: bucket NONE — skip")
        return
    # Versioning is on, so plain deletes leave every prior version behind and the bucket
    # will not delete. Both versions and delete markers have to go.
    paginator = s3.get_paginator("list_object_versions")
    removed = 0
    for page in paginator.paginate(Bucket=bucket):
        batch = [
            {"Key": o["Key"], "VersionId": o["VersionId"]}
            for o in list(page.get("Versions", [])) + list(page.get("DeleteMarkers", []))
        ]
        for i in range(0, len(batch), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch[i : i + 1000]})
            removed += len(batch[i : i + 1000])
    s3.delete_bucket(Bucket=bucket)
    log(f"{region}: bucket deleted: {bucket} ({removed} object versions removed)")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Provision Bedrock model invocation logging (runtime plane only)"
    )
    ap.add_argument("command", nargs="?", default="deploy", choices=["deploy", "status", "teardown"])
    ap.add_argument(
        "--regions",
        default=",".join(DEFAULT_REGIONS),
        help=f"comma-separated Regions (default: {','.join(DEFAULT_REGIONS)})",
    )
    ap.add_argument("--log-group", default=DEFAULT_LOG_GROUP)
    ap.add_argument("--retention-days", type=int, default=30)
    ap.add_argument("--lifecycle-days", type=int, default=90, help="S3 expiry for large bodies")
    ap.add_argument(
        "--modalities",
        default="text",
        help="comma-separated: text,image,embedding,video (default: text). "
        "image/video store binary payloads in S3 — enable deliberately.",
    )
    ap.add_argument("--kms-key-arn", default=None, help="CMK for the sidecar bucket (default SSE-S3)")
    ap.add_argument("--allow-seoul", action="store_true", help=f"permit {NEGATIVE_CONTROL_REGION}")
    ap.add_argument("--allow-non-dev", action="store_true", help="permit ENVIRONMENT != dev")
    ap.add_argument("--delete-log-group", action="store_true", help="teardown: destroy log records")
    ap.add_argument("--delete-bucket", action="store_true", help="teardown: destroy stored bodies")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    args.regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    if not args.regions:
        fail("--regions must name at least one Region")

    if args.command == "deploy":
        cmd_deploy(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        cmd_teardown(args)


if __name__ == "__main__":
    main()
