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

variable "topdog_user" {
  type    = string
  default = ""
}

variable "topdog_pw" {
  type    = string
  default = ""
}

variable "openrouter_enabled" {
  type    = string
  default = "false"
}

variable "openrouter_api_key" {
  type    = string
  default = ""
}

variable "openrouter_model" {
  type    = string
  default = ""
}

variable "openrouter_pdf_engine" {
  type    = string
  default = "mistral-ocr"
}

variable "openrouter_pdf_pages_per_chunk" {
  type    = string
  default = "8"
}

variable "openrouter_max_tokens" {
  type    = string
  default = "32768"
}

variable "openrouter_max_concurrency" {
  type    = string
  default = "3"
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
      port "postgres" { to = 5432 }
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

//    meta {
  //    nomad_ingress_enabled  = true
    //  nomad_ingress_hostname = "bar-hopping-db.service.consul"
   // }

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
      port "redis" { to = 6379 }
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
      port "http" { to = 8000 }
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
        #  nomad_ingress_hostname = "bar-hopping.service.consul"
        nomad_ingress_hostname = "bar-hopping.secure.carey.id"
      }
    }
    
    task "wait-for-db" {
      lifecycle {
        hook    = "prestart"
        sidecar = false
      }
      driver = "docker"
      config {
        image        = "alpine"
        network_mode = "host"
        command      = "sh"
        args         = ["-c", "until wget -qO- 'http://127.0.0.1:8500/v1/health/service/bar-hopping-db?passing=true' 2>/dev/null | grep -q ServiceID; do echo 'waiting for bar-hopping-db...'; sleep 60; done"]
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
        UVICORN_LOG_LEVEL   = "debug"

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

    task "worker" {
      driver = "docker"

      config {
        image      = "ghcr.io/pcareyrh/bar-hopping:main"
        force_pull = true
      }

      env {
        ENCRYPTION_KEY        = var.encryption_key
        DB_PASSWORD           = var.db_password
        TOPDOG_USER           = var.topdog_user
        TOPDOG_PW             = var.topdog_pw
        OPENROUTER_ENABLED    = var.openrouter_enabled
        OPENROUTER_API_KEY    = var.openrouter_api_key
        OPENROUTER_MODEL      = var.openrouter_model
        OPENROUTER_PDF_ENGINE = var.openrouter_pdf_engine
        OPENROUTER_PDF_PAGES_PER_CHUNK = var.openrouter_pdf_pages_per_chunk
        OPENROUTER_MAX_TOKENS = var.openrouter_max_tokens
        OPENROUTER_MAX_CONCURRENCY = var.openrouter_max_concurrency
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
