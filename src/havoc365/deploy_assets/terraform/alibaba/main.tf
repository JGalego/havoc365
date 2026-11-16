terraform {
  required_version = ">= 1.6.0"
  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = "~> 1.284"
    }
  }
}

provider "alicloud" {
  region = var.region
}

resource "alicloud_vpc" "lifeboat" {
  vpc_name   = var.name
  cidr_block = "10.77.0.0/16"
}

resource "alicloud_vswitch" "lifeboat" {
  vswitch_name = var.name
  vpc_id       = alicloud_vpc.lifeboat.id
  cidr_block   = "10.77.0.0/24"
  zone_id      = var.zone
}

resource "alicloud_security_group" "lifeboat" {
  security_group_name = var.name
  vpc_id              = alicloud_vpc.lifeboat.id
}

resource "alicloud_security_group_rule" "ssh" {
  type              = "ingress"
  ip_protocol       = "tcp"
  nic_type          = "intranet"
  policy            = "accept"
  port_range        = "22/22"
  priority          = 1
  security_group_id = alicloud_security_group.lifeboat.id
  cidr_ip           = var.ssh_cidr
}

resource "alicloud_security_group_rule" "web" {
  for_each          = toset(["80", "443"])
  type              = "ingress"
  ip_protocol       = "tcp"
  nic_type          = "intranet"
  policy            = "accept"
  port_range        = "${each.value}/${each.value}"
  priority          = 1
  security_group_id = alicloud_security_group.lifeboat.id
  cidr_ip           = "0.0.0.0/0"
}

resource "alicloud_key_pair" "lifeboat" {
  key_pair_name = var.name
  public_key    = var.public_key
}

resource "alicloud_instance" "lifeboat" {
  instance_name              = var.name
  image_id                   = var.image
  instance_type              = var.instance_type
  availability_zone          = var.zone
  vswitch_id                 = alicloud_vswitch.lifeboat.id
  security_groups            = [alicloud_security_group.lifeboat.id]
  key_name                   = alicloud_key_pair.lifeboat.key_pair_name
  instance_charge_type       = "PostPaid"
  internet_charge_type       = "PayByTraffic"
  internet_max_bandwidth_out = 5
  system_disk_category       = "cloud_essd"
  system_disk_size           = 30
  system_disk_encrypted      = true
  deletion_protection        = true
  http_tokens                = "required"
  dynamic "data_disks" {
    for_each = var.state_disk == "yes" ? [1] : []
    content {
      name                 = "${var.name}-state"
      size                 = 20
      category             = "cloud_essd"
      encrypted            = true
      delete_with_instance = false
      device               = "/dev/vdb"
    }
  }
  lifecycle {
    prevent_destroy = true
  }
}

output "host" {
  value = alicloud_instance.lifeboat.public_ip
}
