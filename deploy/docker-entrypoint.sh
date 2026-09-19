#!/bin/sh
set -eu

mkdir -p /app/data /app/output/applications /app/output/applied /app/.browser_profile

seed_file() {
    destination="$1"
    source="/opt/job-applier-defaults/$1"

    if [ ! -e "/app/$destination" ]; then
        cp "$source" "/app/$destination"
        echo "Initialized $destination from the container defaults."
    fi
}

seed_file data/candidate_profile.json
seed_file data/master_resume.json

if [ -s /opt/job-applier-defaults/linkedin_cookies.db ] && [ ! -e /app/.browser_profile/Default/Network/Cookies ]; then
    mkdir -p /app/.browser_profile/Default/Network
    cp /opt/job-applier-defaults/linkedin_cookies.db /app/.browser_profile/Default/Network/Cookies
    echo "Seeded LinkedIn session cookies from host extraction."
fi

exec "$@"
