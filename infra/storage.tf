resource "google_storage_bucket" "pipeline_data" {
  name                        = "pbmc-okf-portfolio-data"
  location                    = "europe-west1"
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