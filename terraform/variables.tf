variable "project_id" {
  type        = string
  description = "The GCP Project ID"
}

variable "region" {
  type        = string
  description = "The GCP Region (e.g., asia-south1)"
  default     = "asia-south1"
}
