variable "encryption_key" {
  type = string
}

variable "db_password" {
  type = string
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
      }

      volume_mount {
        volume      = "bar-hopping-data"
        destination = "/var/lib/postgresql/data"
        read_only   = false
      }

      resources {
        cpu    = 256
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

    task "web" {
      driver = "docker"

      config {
        image      = "ghcr.io/pcareyrh/bar-hopping-web:main"
        ports      = ["http"]
        force_pull = true
      }

      env {
        DATABASE_URL   = var.database_url
        REDIS_URL      = var.redis_url
        ENCRYPTION_KEY = var.encryption_key
      }

      resources {
        cpu    = 512
        memory = 512
      }
    }

    task "worker" {
      driver = "docker"

      config {
        image      = "ghcr.io/pcareyrh/bar-hopping:main"
        force_pull = true
      }

      env {
        DATABASE_URL   = var.database_url
        REDIS_URL      = var.redis_url
        ENCRYPTION_KEY = var.encryption_key
      }

      resources {
        cpu    = 2048
        memory = 2048
      }
    }
  }
}
