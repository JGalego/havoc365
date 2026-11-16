terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
}

resource "aws_key_pair" "lifeboat" {
  key_name_prefix = "${var.name}-"
  public_key      = var.public_key
}

resource "aws_security_group" "lifeboat" {
  name_prefix = "${var.name}-"
  description = "Lifeboat HTTPS and restricted administrator SSH"
  vpc_id      = var.vpc_id
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_cidr]
  }
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "lifeboat" {
  ami                         = var.image
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  key_name                    = aws_key_pair.lifeboat.key_name
  vpc_security_group_ids      = [aws_security_group.lifeboat.id]
  associate_public_ip_address = true
  disable_api_termination     = true
  metadata_options {
    http_tokens = "required"
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    encrypted             = true
    delete_on_termination = false
  }
  tags = { Name = var.name }
  lifecycle {
    prevent_destroy = true
  }
}

output "host" {
  value = aws_instance.lifeboat.public_ip
}
