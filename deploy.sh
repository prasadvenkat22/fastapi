#!/bin/bash
# Deployment script for Digital Ocean droplet
# Usage: ./deploy.sh [command]
# Commands:
#   start     - Start all services
#   stop      - Stop all services
#   restart   - Restart all services
#   update    - Pull latest code and rebuild
#   logs      - Show logs (follow mode)
#   status    - Show container status
#   stats     - Show resource usage
#   debug     - Start with pgAdmin enabled

set -e

COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml"
ENV_FILE=".env.production"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_env() {
    if [ ! -f "$ENV_FILE" ]; then
        log_error "Production environment file not found: $ENV_FILE"
        log_info "Copy .env.production and update with your values"
        exit 1
    fi
}

start() {
    check_env
    log_info "Starting services..."
    docker compose $COMPOSE_FILES --env-file $ENV_FILE up -d
    log_info "Services started. Checking status..."
    sleep 3
    status
}

stop() {
    log_info "Stopping services..."
    docker compose $COMPOSE_FILES down
    log_info "Services stopped."
}

restart() {
    log_info "Restarting services..."
    stop
    start
}

update() {
    log_info "Pulling latest code..."
    git pull origin main

    log_info "Rebuilding containers..."
    docker compose $COMPOSE_FILES --env-file $ENV_FILE build --no-cache

    log_info "Restarting services with new build..."
    docker compose $COMPOSE_FILES --env-file $ENV_FILE up -d

    log_info "Cleaning up old images..."
    docker image prune -f

    log_info "Update complete. Checking status..."
    sleep 3
    status
}

logs() {
    docker compose $COMPOSE_FILES logs -f
}

status() {
    log_info "Container status:"
    docker compose $COMPOSE_FILES ps
    echo ""
    log_info "Memory usage:"
    free -h
}

stats() {
    log_info "Live resource usage (Ctrl+C to exit):"
    docker stats
}

debug() {
    check_env
    log_info "Starting services with pgAdmin enabled..."
    docker compose $COMPOSE_FILES --env-file $ENV_FILE --profile debug up -d
    log_info "pgAdmin available at http://localhost:5050"
    status
}

health_check() {
    log_info "Running health checks..."

    echo -n "FastAPI: "
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/docs | grep -q "200"; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAILED${NC}"
    fi

    echo -n "Streamlit: "
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:8501 | grep -q "200"; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAILED${NC}"
    fi

    echo -n "PostgreSQL: "
    if docker compose $COMPOSE_FILES exec -T pgdb pg_isready -U postgres > /dev/null 2>&1; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAILED${NC}"
    fi

    echo -n "MongoDB: "
    if docker compose $COMPOSE_FILES exec -T mongo mongosh --eval "db.adminCommand('ping')" > /dev/null 2>&1; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAILED${NC}"
    fi
}

show_help() {
    echo "FastAPI Deployment Script"
    echo ""
    echo "Usage: ./deploy.sh [command]"
    echo ""
    echo "Commands:"
    echo "  start      Start all production services"
    echo "  stop       Stop all services"
    echo "  restart    Restart all services"
    echo "  update     Pull latest code and rebuild containers"
    echo "  logs       Show logs (follow mode)"
    echo "  status     Show container status and memory usage"
    echo "  stats      Show live resource usage"
    echo "  debug      Start with pgAdmin enabled"
    echo "  health     Run health checks on all services"
    echo "  help       Show this help message"
    echo ""
    echo "Examples:"
    echo "  ./deploy.sh start    # Start production stack"
    echo "  ./deploy.sh update   # Deploy latest changes"
    echo "  ./deploy.sh logs     # View logs"
}

# Main command handler
case "${1:-help}" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    update)
        update
        ;;
    logs)
        logs
        ;;
    status)
        status
        ;;
    stats)
        stats
        ;;
    debug)
        debug
        ;;
    health)
        health_check
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        log_error "Unknown command: $1"
        show_help
        exit 1
        ;;
esac
