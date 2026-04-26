job "bar-hopping-results-cron" {
  datacenters = ["dc1"]
  type        = "batch"

  periodic {
    cron             = "0 3 * * 1"
    time_zone        = "Australia/Sydney"
    prohibit_overlap = true
  }

  group "enqueue" {
    count = 1

    task "trigger" {
      driver = "docker"

      config {
        # Worker image — has python, redis client, rq, and app code.
        image      = "ghcr.io/pcareyrh/bar-hopping:main"
        force_pull = true
        command    = "python3"
        args       = [
          "-c",
          "from app.queue import get_queue; print(get_queue().enqueue('app.worker.weekly_results_refresh_job', job_timeout=3600).id)"
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
