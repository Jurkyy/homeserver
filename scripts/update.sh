#!/bin/bash
set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Change to script directory so paths work
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# Parse arguments
FORCE_RECREATE=""
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --force|-f) FORCE_RECREATE="--force-recreate" ;;
        *) echo "Unknown parameter: $1"; exit 1 ;;
    esac
    shift
done

echo -e "${BLUE}=== Service Update ===${NC}"
echo ""

# Check if docker compose file exists
if [ ! -f "docker-compose.yml" ]; then
    echo -e "${RED}Error: docker-compose.yml not found${NC}"
    exit 1
fi

# Pull latest images
echo -e "${BLUE}Pulling latest images...${NC}"
docker compose pull

echo ""
echo -e "${BLUE}Recreating containers with new images...${NC}"
if [ -n "$FORCE_RECREATE" ]; then
    echo -e "${YELLOW}Force recreate enabled${NC}"
fi
docker compose up -d $FORCE_RECREATE

echo ""
echo -e "${BLUE}Container status:${NC}"
docker compose ps

echo ""
echo -e "${GREEN}=== Update Complete ===${NC}"
