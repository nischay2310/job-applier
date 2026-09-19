#!/bin/bash
# One-time LinkedIn login setup for Docker
# Run: docker exec job-applier-app-1 bash /app/login.sh

set -e

echo "================================================"
echo "  LinkedIn Login Setup (One-time)"
echo "================================================"
echo ""

# Check if already logged in
python src/job_applier/cli/auth_cli.py status

echo ""
echo "To login to LinkedIn:"
echo ""
echo "Method 1: Using Playwright (Automated, Headless)"
echo "  docker exec -it job-applier-app-1 python src/job_applier/cli/auth_cli.py login linkedin"
echo ""
echo "Method 2: Export cookies from your desktop Chrome and import"
echo "  (Detailed instructions below)"
echo ""
echo "================================================"
echo "Method 2 Steps:"
echo "================================================"
echo "1. Copy Chrome Cookies database from your Windows PC:"
echo "   Source: C:\Users\Nisch\AppData\Local\Google\Chrome\User Data\Default\Network\Cookies"
echo ""
echo "2. Copy into Docker:"
echo "   docker cp 'C:\Users\Nisch\AppData\Local\Google\Chrome\User Data\Default\Network\Cookies' job-applier-app-1:/app/.browser_profile/Default/Network/"
echo ""
echo "3. Verify login:"
echo "   docker exec job-applier-app-1 python src/job_applier/cli/auth_cli.py status"
echo ""
echo "Done! Your LinkedIn session is now saved in Docker."
echo ""
