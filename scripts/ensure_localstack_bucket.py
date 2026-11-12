#!/usr/bin/env python3
"""Ensure the LocalStack development S3 bucket exists.

Creates the bucket (idempotently), enables versioning and blocks public access,
mirroring the configuration the backend expects in development (see
``.env.development`` and ``platform/backend/app/storage/s3.py``).

Configuration comes from CLI flags, each with a sensible LocalStack default, so
it can be driven from the Makefile or run by hand.
"""
from __future__ import annotations

import argparse
import sys

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
except ImportError as exc:  # pragma: no cover - surfaced to the operator
    sys.stderr.write(
        "boto3/botocore are required for this script. Run it with the backend "
        "venv (platform/backend/.venv) which provides them. "
        f"({exc})\n"
    )
    raise SystemExit(1)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Create/verify the LocalStack development S3 bucket."
    )
    p.add_argument("--port", default="4566", help="LocalStack S3 port (default: 4566).")
    p.add_argument("--bucket", default="quant-platform-dev",
                   help="Bucket name (default: quant-platform-dev).")
    p.add_argument("--user", default="localstack", help="Access key id (default: localstack).")
    p.add_argument("--pass", dest="password", default="testpass",
                   help="Secret access key (default: testpass).")
    p.add_argument("--region", default="ap-south-1",
                   help="AWS region (default: ap-south-1).")
    p.add_argument("--endpoint", default=None,
                   help="Override endpoint URL (default: http://localhost:<port>).")
    p.add_argument("--addressing-style", default="path",
                   help="S3 addressing style; LocalStack needs 'path'.")
    args = p.parse_args(argv)

    endpoint = args.endpoint or "http://localhost:{}".format(args.port)
    bucket = args.bucket

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=args.region,
        aws_access_key_id=args.user,
        aws_secret_access_key=args.password,
        config=Config(s3={"addressing_style": args.addressing_style}),
    )

    print("Ensuring LocalStack bucket '{}' on {} ...".format(bucket, endpoint))

    # 1) Bucket existence (idempotent create).
    # A non us-east-1 region (LocalStack defaults to ap-south-1 here) requires
    # a LocationConstraint, else CreateBucket raises IllegalLocationConstraintException;
    # us-east-1 must omit it or it errors the other way round.
    create_kwargs = {"Bucket": bucket}
    if args.region and args.region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {
            "LocationConstraint": args.region
        }
    try:
        client.create_bucket(**create_kwargs)
        print("created bucket: {}".format(bucket))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            print("bucket already exists: {}".format(bucket))
        else:
            raise

    # 2) Versioning.
    try:
        client.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        print("versioning: enabled")
    except ClientError as exc:
        print("versioning: {}".format(exc), file=sys.stderr)

    # 3) Public access block. The operation name varies across botocore
    #    versions: newer models expose 'put_public_access_block', older ones
    #    'put_bucket_public_access_block'; call whichever the client provides.
    pab_cfg = {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    pab_done = False
    for _pab in ("put_public_access_block", "put_bucket_public_access_block"):
        if not hasattr(client, _pab):
            continue
        try:
            getattr(client, _pab)(
                Bucket=bucket, PublicAccessBlockConfiguration=pab_cfg
            )
            print("public-access-block: enabled ({})".format(_pab))
            pab_done = True
            break
        except ClientError as exc:
            print("public-access-block: {}".format(exc), file=sys.stderr)
    if not pab_done:
        print("public-access-block: not supported by this client",
              file=sys.stderr)

    print("bucket ready: {}".format(bucket))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
