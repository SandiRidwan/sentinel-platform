# SENTINEL lake — four zone buckets with the lifecycle rules that make retention real.
#
# The lifecycle rules are not decoration: governance/policies/column_security.yml declares
# 90 days for raw and 730 for gold, and this is where that declaration becomes enforced by
# the platform rather than by a promise in a document.

resource "aws_s3_bucket" "zone" {
  for_each = var.buckets

  bucket        = each.value
  force_destroy = var.force_destroy
  tags          = merge(var.tags, { zone = each.key })
}

# Versioning: an accidental overwrite of a partition is recoverable rather than final.
resource "aws_s3_bucket_versioning" "zone" {
  for_each = var.buckets

  bucket = aws_s3_bucket.zone[each.key].id
  versioning_configuration {
    status = "Enabled"
  }
}

# Encrypt at rest. SSE-S3 rather than SSE-KMS: no key policy to get wrong, and the threat
# this defends against is disk access, not a compromised account.
resource "aws_s3_bucket_server_side_encryption_configuration" "zone" {
  for_each = var.buckets

  bucket = aws_s3_bucket.zone[each.key].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Public access blocked on every bucket. There is no case in this platform where a lake
# object should be public; the serving layer is ClickHouse, not S3.
resource "aws_s3_bucket_public_access_block" "zone" {
  for_each = var.buckets

  bucket = aws_s3_bucket.zone[each.key].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "zone" {
  for_each = var.buckets

  bucket = aws_s3_bucket.zone[each.key].id

  rule {
    id     = "expire-noncurrent"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  # raw is re-scrapable, so it is dropped on a schedule. gold is the system of record and
  # only moves to cheaper storage.
  dynamic "rule" {
    for_each = each.key == "raw" ? [1] : []
    content {
      id     = "expire-raw"
      status = "Enabled"

      filter {}

      expiration {
        days = var.raw_days
      }
    }
  }

  dynamic "rule" {
    for_each = each.key == "gold" ? [1] : []
    content {
      id     = "transition-gold"
      status = "Enabled"

      filter {}

      transition {
        days          = 90
        storage_class = "STANDARD_IA"
      }

      expiration {
        days = var.gold_days
      }
    }
  }
}

output "bucket_names" {
  value = { for zone, bucket in aws_s3_bucket.zone : zone => bucket.bucket }
}

output "bucket_arns" {
  value = { for zone, bucket in aws_s3_bucket.zone : zone => bucket.arn }
}
