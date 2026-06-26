job "bar-hopping-live-cron" {
  datacenters = ["dc1"]
  type        = "batch"

  periodic {
    cron             = "*/5 7-18 * * *"
    time_zone        = "Australia/Sydney"
    prohibit_overlap = true
  }

  group "enqueue" {
    count = 1

    task "trigger" {
      driver = "docker"

      config {
        image      = "ghcr.io/pcareyrh/bar-hopping:main"
        force_pull = true
        command    = "python3"
        args       = [
          "-c",
          "from app.queue import get_queue; print(get_queue().enqueue('app.worker.sweep_live_trials_job', job_timeout=300).id)"
        ]
      }

      template {
        data = <<EOH
{{ range service "bar-hopping-redis" -}}
REDIS_URL="redis://{{ .Address }}:{{ .Port }}"
{{ end -}}
EOH
        destination = "secrets/cron.env"
        env         = true
      }

      resources {
        cpu    = 100
        memory = 128
      }
    }
  }
}
