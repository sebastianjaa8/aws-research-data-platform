terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region"   { default = "us-east-1" }
variable "project_name" { default = "research-data-platform" }
variable "db_password"  { sensitive = true }

# ─── S3 — raw instrument data ────────────────────────────────────────────────

resource "aws_s3_bucket" "raw_data" {
  bucket        = "${var.project_name}-raw-data"
  force_destroy = false

  tags = { Project = var.project_name }
}

resource "aws_s3_bucket_versioning" "raw_data" {
  bucket = aws_s3_bucket.raw_data.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw_data" {
  bucket = aws_s3_bucket.raw_data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "raw_data" {
  bucket                  = aws_s3_bucket.raw_data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ─── Lambda — S3 event → ECS dispatch ────────────────────────────────────────

resource "aws_iam_role" "lambda_exec" {
  name = "${var.project_name}-lambda-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_ecs" {
  name = "ecs-run-task"
  role = aws_iam_role.lambda_exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ecs:RunTask", "iam:PassRole"]
      Resource = "*"
    }]
  })
}

resource "aws_lambda_function" "ingestion_dispatcher" {
  function_name = "${var.project_name}-ingestion-dispatcher"
  role          = aws_iam_role.lambda_exec.arn
  runtime       = "python3.12"
  handler       = "lambda_handler.handler"
  filename      = "${path.module}/../src/ingestion/lambda_handler.zip"
  timeout       = 30

  environment {
    variables = {
      CLUSTER   = aws_ecs_cluster.processing.name
      TASK_DEF  = aws_ecs_task_definition.decoder.arn
    }
  }

  tags = { Project = var.project_name }
}

resource "aws_s3_bucket_notification" "ingestion_trigger" {
  bucket = aws_s3_bucket.raw_data.id
  lambda_function {
    lambda_function_arn = aws_lambda_function.ingestion_dispatcher.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "raw/"
  }
  depends_on = [aws_lambda_permission.allow_s3]
}

resource "aws_lambda_permission" "allow_s3" {
  statement_id  = "AllowExecutionFromS3"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingestion_dispatcher.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.raw_data.arn
}

# ─── ECS Fargate — binary decoder worker ─────────────────────────────────────

resource "aws_ecs_cluster" "processing" {
  name = "${var.project_name}-processing"
  tags = { Project = var.project_name }
}

resource "aws_iam_role" "ecs_task_exec" {
  name = "${var.project_name}-ecs-task-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_exec_policy" {
  role       = aws_iam_role.ecs_task_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_bedrock_s3" {
  name = "bedrock-s3-access"
  role = aws_iam_role.ecs_task_exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.raw_data.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.db_credentials.arn
      }
    ]
  })
}

resource "aws_ecs_task_definition" "decoder" {
  family                   = "${var.project_name}-decoder"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_task_exec.arn
  task_role_arn            = aws_iam_role.ecs_task_exec.arn

  container_definitions = jsonencode([{
    name      = "decoder"
    image     = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${var.project_name}-decoder:latest"
    essential = true
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "DB_SECRET_ARN", value = aws_secretsmanager_secret.db_credentials.arn }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/ecs/${var.project_name}"
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "decoder"
      }
    }
  }])
}

data "aws_caller_identity" "current" {}

# ─── RDS PostgreSQL + pgvector ────────────────────────────────────────────────

resource "aws_db_subnet_group" "main" {
  name       = "${var.project_name}-db-subnet"
  subnet_ids = aws_subnet.private[*].id
  tags       = { Project = var.project_name }
}

resource "aws_db_instance" "postgres" {
  identifier             = "${var.project_name}-db"
  engine                 = "postgres"
  engine_version         = "16.2"
  instance_class         = "db.t3.medium"
  allocated_storage      = 100
  max_allocated_storage  = 500
  db_name                = "research_platform"
  username               = "platform_admin"
  password               = var.db_password
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  storage_encrypted      = true
  backup_retention_period = 7
  skip_final_snapshot    = false
  final_snapshot_identifier = "${var.project_name}-final-snapshot"

  tags = { Project = var.project_name }
}

# ─── Secrets Manager ─────────────────────────────────────────────────────────

resource "aws_secretsmanager_secret" "db_credentials" {
  name = "${var.project_name}/db-credentials"
  tags = { Project = var.project_name }
}

resource "aws_secretsmanager_secret_version" "db_credentials" {
  secret_id = aws_secretsmanager_secret.db_credentials.id
  secret_string = jsonencode({
    host     = aws_db_instance.postgres.address
    port     = 5432
    dbname   = "research_platform"
    username = "platform_admin"
    password = var.db_password
  })
}

# ─── VPC (minimal — adapt subnets to your account) ───────────────────────────

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  tags = { Name = "${var.project_name}-vpc", Project = var.project_name }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.${count.index}.0/24"
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags = { Name = "${var.project_name}-private-${count.index}", Project = var.project_name }
}

data "aws_availability_zones" "available" { state = "available" }

resource "aws_security_group" "db" {
  name   = "${var.project_name}-db-sg"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Project = var.project_name }
}

# ─── Outputs ──────────────────────────────────────────────────────────────────

output "s3_bucket_name"     { value = aws_s3_bucket.raw_data.bucket }
output "ecs_cluster_name"   { value = aws_ecs_cluster.processing.name }
output "db_endpoint"        { value = aws_db_instance.postgres.address }
output "db_secret_arn"      { value = aws_secretsmanager_secret.db_credentials.arn }
