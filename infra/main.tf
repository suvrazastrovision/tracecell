terraform {
  required_version = ">= 1.16.0, < 2.0.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
  }
}

provider "google" {
  project = "pbmc-okf-portfolio"
  region  = "europe-west1"
}

resource "google_storage_bucket" "terraform_state" {
  name                        = "pbmc-okf-portfolio-tfstate"
  location                    = "europe-west1"
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }
}

output "terraform_state_bucket" {
  value = google_storage_bucket.terraform_state.name
}