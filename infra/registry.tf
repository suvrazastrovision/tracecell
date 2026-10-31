resource "google_artifact_registry_repository" "containers" {
  location      = "europe-west1"
  repository_id = "pbmc-containers"
  description   = "Container images for the PBMC data platform"
  format        = "DOCKER"

  docker_config {
    immutable_tags = true
  }
}