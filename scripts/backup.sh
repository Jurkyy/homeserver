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

# Configuration
BACKUP_DIR="backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_NAME="config_backup_${TIMESTAMP}.tar.gz"
KEEP_BACKUPS=7

echo -e "${BLUE}=== Service Config Backup ===${NC}"

# Create backups directory if it doesn't exist
if [ ! -d "$BACKUP_DIR" ]; then
    echo -e "${YELLOW}Creating backups directory...${NC}"
    mkdir -p "$BACKUP_DIR"
fi

# Check if services directory exists
if [ ! -d "services" ]; then
    echo -e "${RED}Error: services directory not found${NC}"
    exit 1
fi

# Find all config directories, excluding cache directories
echo -e "${BLUE}Collecting config directories...${NC}"

# Create the backup, excluding cache directories
echo -e "${BLUE}Creating backup...${NC}"
tar --exclude='*cache*' \
    --exclude='*Cache*' \
    --exclude='*.cache' \
    -czf "${BACKUP_DIR}/${BACKUP_NAME}" \
    services/*/config 2>/dev/null || {
    # If no config directories found, create empty backup note
    if [ $? -eq 1 ]; then
        echo -e "${YELLOW}Warning: No config directories found to backup${NC}"
        exit 0
    fi
}

echo -e "${GREEN}Backup created successfully!${NC}"

# Clean up old backups (keep last 7)
echo -e "${BLUE}Cleaning up old backups...${NC}"
BACKUP_COUNT=$(ls -1 "${BACKUP_DIR}"/config_backup_*.tar.gz 2>/dev/null | wc -l)

if [ "$BACKUP_COUNT" -gt "$KEEP_BACKUPS" ]; then
    DELETE_COUNT=$((BACKUP_COUNT - KEEP_BACKUPS))
    echo -e "${YELLOW}Removing ${DELETE_COUNT} old backup(s)...${NC}"
    ls -1t "${BACKUP_DIR}"/config_backup_*.tar.gz | tail -n "$DELETE_COUNT" | xargs rm -f
    echo -e "${GREEN}Old backups removed${NC}"
else
    echo -e "${GREEN}No old backups to remove (${BACKUP_COUNT}/${KEEP_BACKUPS} backups)${NC}"
fi

# Print backup location
FULL_PATH="$(pwd)/${BACKUP_DIR}/${BACKUP_NAME}"
echo ""
echo -e "${GREEN}=== Backup Complete ===${NC}"
echo -e "${GREEN}Location: ${FULL_PATH}${NC}"
echo -e "${GREEN}Size: $(du -h "${BACKUP_DIR}/${BACKUP_NAME}" | cut -f1)${NC}"
