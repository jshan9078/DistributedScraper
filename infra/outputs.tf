output "artifact_registry_repo" { value = "${var.region}-docker.pkg.dev/${var.project_id}/psa-repo" }
output "gcs_bucket"            { value = google_storage_bucket.dataset.name }
output "cloudsql_instance"     { value = google_sql_database_instance.psa.connection_name }
output "gke_cluster"           { value = google_container_cluster.cluster.name }
