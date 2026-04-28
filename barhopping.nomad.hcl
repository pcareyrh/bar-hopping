variable "encryption_key" {
  type = string
  default = "eWVv7d1obaYl9oY2XQWbSAS923jw-zEqWQlb219Vc4w="
}

variable "db_password" {
  type = string
  default = "postgres"
}

variable "results_admin_token" {
  type = string
  default = "postgrestoken"
}

variable "database_url" {
  type    = string
  default = "postgresql+psycopg2://barhopping:changeme@bar-hopping-db.service.consul/barhopping"
}

variable "redis_url" {
  type    = string
  default = "redis://bar-hopping-redis.service.consul:6379"
}

job "bar-hopping" {
  datacenters = ["dc1"]
  type        = "service"
  node_pool   = "nfs-nodes"

  update {
    min_healthy_time  = "10s"
    healthy_deadline  = "15m"
    progress_deadline = "300m"
  }
  # Postgres group — persistent database on the existing CSI volume
  group "db" {
    count = 1

    volume "bar-hopping-data" {
      type            = "csi"
      source          = "barhoppingdata"
      read_only       = false
      attachment_mode = "file-system"
      access_mode     = "single-node-writer"
    }

    network {
      port "postgres" { static = 5432 }
    }

    service {
      name = "bar-hopping-db"
      port = "postgres"

      check {
        type     = "tcp"
        interval = "10s"
        timeout  = "5s"
      }
    }

    task "postgres" {
      driver = "docker"

      config {
        image = "postgres:16-alpine"
        ports = ["postgres"]
      }

      env {
        POSTGRES_DB       = "barhopping"
        POSTGRES_USER     = "barhopping"
        POSTGRES_PASSWORD = var.db_password
        PGDATA            = "/var/lib/postgresql/data/pgdata"
      }

      volume_mount {
        volume      = "bar-hopping-data"
        destination = "/var/lib/postgresql/data"
        read_only   = false
      }

      resources {
        cpu    = 512
        memory = 512
      }
    }
  }

  # Redis group — ephemeral job queue broker
  group "redis" {
    count = 1

    network {
      port "redis" { static = 6379 }
    }

    service {
      name = "bar-hopping-redis"
      port = "redis"

      check {
        type     = "tcp"
        interval = "10s"
        timeout  = "5s"
      }
    }

    task "redis" {
      driver = "docker"

      config {
        image = "redis:7-alpine"
        ports = ["redis"]
      }

      resources {
        cpu    = 128
        memory = 128
      }
    }
  }

  # App group — web server + Playwright worker
  group "app" {
    count = 1

    network {
      port "http" { static = 8000 }
    }

    update {
      healthy_deadline  = "15m"
      progress_deadline = "25m"
    }

    service {
      name = "bar-hopping"
      port = "http"

      check {
        type     = "tcp"
        interval = "30s"
        timeout  = "10s"
      }

      meta {
        nomad_ingress_enabled  = true
        nomad_ingress_hostname = "bar-hopping.service.consul"
      }
    }

    task "wait-for-db" {
      # Polls Consul's HTTP API directly (no DNS needed) until the db service
      # is registered and passing its health check. Blocks web + worker from
      # starting until Postgres is ready.
      lifecycle {
        hook    = "prestart"
        sidecar = false
      }
      driver = "docker"
      config {
        image        = "alpine"
        network_mode = "host"
        command      = "sh"
        args         = ["-c", "until wget -qO- 'http://127.0.0.1:8500/v1/health/service/bar-hopping-db?passing=true' 2>/dev/null | grep -q ServiceID; do echo 'waiting for bar-hopping-db...'; sleep 2; done"]
      }
      resources {
        cpu    = 50
        memory = 64
      }
    }

    task "web" {
      driver = "docker"

      config {
        image      = "ghcr.io/pcareyrh/bar-hopping-web:main"
        ports      = ["http"]
        force_pull = true
      }

      env {
        ENCRYPTION_KEY      = var.encryption_key
        DB_PASSWORD         = var.db_password
        RESULTS_ADMIN_TOKEN = var.results_admin_token
        LOG_LEVEL           = "debug"
      }

      # Consul template runs in the Nomad client process (not in the container)
      # so it resolves bar-hopping-db via the local Consul agent directly —
      # no .consul DNS required inside the container.
      template {
        data = <<EOH
{{ range service "bar-hopping-db" -}}
DATABASE_URL="postgresql+psycopg2://barhopping:{{ env "DB_PASSWORD" }}@{{ .Address }}:{{ .Port }}/barhopping"
{{ end -}}
{{ range service "bar-hopping-redis" -}}
REDIS_URL="redis://{{ .Address }}:{{ .Port }}"
{{ end -}}
EOH
        destination = "secrets/app.env"
        env         = true
        change_mode = "restart"
      }

      resources {
        cpu    = 1024
        memory = 1024
      }
    }

    task "worker" {
      driver = "docker"

      config {
        image      = "ghcr.io/pcareyrh/bar-hopping:main"
        force_pull = true
      }

      env {
        ENCRYPTION_KEY = var.encryption_key
        DB_PASSWORD    = var.db_password
      }

      template {
        data = <<EOH
{{ range service "bar-hopping-db" -}}
DATABASE_URL="postgresql+psycopg2://barhopping:{{ env "DB_PASSWORD" }}@{{ .Address }}:{{ .Port }}/barhopping"
{{ end -}}
{{ range service "bar-hopping-redis" -}}
REDIS_URL="redis://{{ .Address }}:{{ .Port }}"
{{ end -}}
EOH
        destination = "secrets/app.env"
        env         = true
        change_mode = "restart"
      }

      resources {
        cpu    = 1024
        memory = 1024
      }
    }
  }
}
