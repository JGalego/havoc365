terraform {
  required_version = ">= 1.6.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
}

provider "google" {
  project = var.project
  region  = var.region
  zone    = var.zone
}

resource "google_compute_network" "lifeboat" {
  name                    = var.name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "lifeboat" {
  name          = var.name
  ip_cidr_range = "10.77.0.0/24"
  region        = var.region
  network       = google_compute_network.lifeboat.id
}

resource "google_compute_firewall" "web" {
  name          = "${var.name}-web"
  network       = google_compute_network.lifeboat.name
  source_ranges = ["0.0.0.0/0"]
  target_tags   = [var.name]
  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
}

resource "google_compute_firewall" "ssh" {
  name          = "${var.name}-ssh"
  network       = google_compute_network.lifeboat.name
  source_ranges = [var.ssh_cidr]
  target_tags   = [var.name]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_instance" "lifeboat" {
  name                = var.name
  machine_type        = var.instance_type
  zone                = var.zone
  deletion_protection = true
  tags                = [var.name]
  boot_disk {
    auto_delete = false
    initialize_params {
      image = var.image
      size  = 30
      type  = "pd-balanced"
    }
  }
  network_interface {
    subnetwork = google_compute_subnetwork.lifeboat.id
    access_config {}
  }
  metadata = {
    ssh-keys               = "${var.ssh_user}:${var.public_key}"
    block-project-ssh-keys = "TRUE"
    enable-oslogin         = "FALSE"
  }
  shielded_instance_config {
    enable_secure_boot = true
  }
  lifecycle {
    prevent_destroy = true
  }
}

output "host" {
  value = google_compute_instance.lifeboat.network_interface[0].access_config[0].nat_ip
}
