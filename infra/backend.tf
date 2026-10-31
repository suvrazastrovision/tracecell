terraform {
  backend "gcs" {
    bucket = "pbmc-okf-portfolio-tfstate"
    prefix = "terraform/state"
  }
}