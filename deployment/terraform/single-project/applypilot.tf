# ApplyPilot-specific infrastructure (rubric 5.2), added on top of the
# agents-cli scaffold (service.tf, iam.tf, storage.tf, telemetry.tf):
#
#   - extra APIs: Secret Manager, Cloud DLP, Model Armor, Artifact Registry
#   - Secret Manager secret `mock-ats-api-key` (random value, never in git)
#   - Model Armor template used by the agent's SafetyGuardrailPlugin
#   - Mock ATS on Cloud Run: own service account, IAM-only (no public access),
#     API key injected from Secret Manager
#   - least-privilege IAM: the agent may invoke ONLY the Mock ATS, read ONLY its
#     own secret, and call DLP / Model Armor
#
# The Mock ATS container image is deployed by `gcloud run deploy --source mock_ats`
# (see README); Terraform owns the service's configuration, not its image.

locals {
  applypilot_services = [
    "secretmanager.googleapis.com",
    "dlp.googleapis.com",
    "modelarmor.googleapis.com",
    "artifactregistry.googleapis.com",
  ]
  model_armor_template_id = "${var.project_name}-guardrails"
}

resource "google_project_service" "applypilot" {
  for_each           = toset(local.applypilot_services)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
  depends_on         = [google_project_service.bootstrap]
}

# --------------------------------------------------------------------------- #
# Secret Manager (rubric 5.3)
# --------------------------------------------------------------------------- #
resource "random_password" "mock_ats_api_key" {
  length  = 40
  special = false
}

resource "google_secret_manager_secret" "mock_ats_api_key" {
  project   = var.project_id
  secret_id = "mock-ats-api-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.applypilot]
}

resource "google_secret_manager_secret_version" "mock_ats_api_key" {
  secret      = google_secret_manager_secret.mock_ats_api_key.id
  secret_data = random_password.mock_ats_api_key.result
}

# Only these two identities can read the key (no project-wide accessor role).
resource "google_secret_manager_secret_iam_member" "agent_reads_ats_key" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.mock_ats_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "ats_reads_ats_key" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.mock_ats_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mock_ats_sa.email}"
}

# --------------------------------------------------------------------------- #
# Model Armor template (rubric 3.3)
# --------------------------------------------------------------------------- #
resource "google_model_armor_template" "guardrails" {
  project     = var.project_id
  location    = var.region
  template_id = local.model_armor_template_id

  filter_config {
    rai_settings {
      rai_filters {
        filter_type      = "HATE_SPEECH"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "HARASSMENT"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "DANGEROUS"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "SEXUALLY_EXPLICIT"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
    }
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = "MEDIUM_AND_ABOVE"
    }
    malicious_uri_filter_settings {
      filter_enforcement = "ENABLED"
    }
  }
  depends_on = [google_project_service.applypilot]
}

# --------------------------------------------------------------------------- #
# Mock ATS on Cloud Run
# --------------------------------------------------------------------------- #
resource "google_service_account" "mock_ats_sa" {
  account_id   = "${var.project_name}-mock-ats"
  display_name = "${var.project_name} Mock ATS Service Account"
  project      = var.project_id
  depends_on   = [google_project_service.services]
}

resource "google_project_iam_member" "mock_ats_sa_roles" {
  for_each = toset(["roles/logging.logWriter", "roles/cloudtrace.agent"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.mock_ats_sa.email}"
}

resource "google_cloud_run_v2_service" "mock_ats" {
  name                = "mock-ats"
  project             = var.project_id
  location            = var.region
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL" # reachable, but IAM still required

  template {
    service_account = google_service_account.mock_ats_sa.email
    scaling {
      min_instance_count = 0
      max_instance_count = 1 # one instance: local SQLite keeps duplicate detection consistent
    }
    containers {
      # Placeholder until `gcloud run deploy mock-ats --source mock_ats` pushes
      # the real image; ignore_changes below keeps Terraform from reverting it.
      image = "us-docker.pkg.dev/cloudrun/container/hello"
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name = "MOCK_ATS_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.mock_ats_api_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }
  depends_on = [
    google_project_service.services,
    google_secret_manager_secret_iam_member.ats_reads_ats_key,
    google_secret_manager_secret_version.mock_ats_api_key,
  ]
}

# Only the agent may call the Mock ATS (no allUsers binding = not public).
resource "google_cloud_run_v2_service_iam_member" "agent_invokes_mock_ats" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.mock_ats.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.app_sa.email}"
}

# --------------------------------------------------------------------------- #
# Outputs used by `agents-cli deploy --env-vars ...`
# --------------------------------------------------------------------------- #
output "mock_ats_url" {
  description = "Set as MOCK_ATS_URL on the agent."
  value       = google_cloud_run_v2_service.mock_ats.uri
}

output "model_armor_template" {
  description = "Set as APPLYPILOT_MODEL_ARMOR_TEMPLATE on the agent."
  value       = "projects/${var.project_id}/locations/${var.region}/templates/${local.model_armor_template_id}"
}

output "agent_service_account" {
  value = google_service_account.app_sa.email
}
