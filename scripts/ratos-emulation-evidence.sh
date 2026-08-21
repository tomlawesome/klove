ratos_archive_runtime() {
    ratos_run_outcome=$1
    ratos_validate_state_tree
    ratos_require_private_file "$ratos_origin" 600
    ratos_require_private_file "$ratos_state_dir/identities.json" 600
    ratos_require_private_file "$ratos_runtime_dir/$ratos_overlay_name" 666
    for ratos_runtime_file in \
        active contract-prepared.json contract-prepare-failure.json contract-succeeded contract.json \
        firstboot-required firstboot-restarted probe-succeeded probe.json
    do
        if [ -e "$ratos_runtime_dir/$ratos_runtime_file" ] \
            || [ -L "$ratos_runtime_dir/$ratos_runtime_file" ]; then
            ratos_require_private_file "$ratos_runtime_dir/$ratos_runtime_file" 600
        fi
    done

    ratos_archive_time=$(date -u +%Y%m%dT%H%M%SZ)
    ratos_archived_run=$(mktemp -d "$ratos_evidence_dir/run-$ratos_archive_time.XXXXXX")
    chmod 0700 "$ratos_archived_run"

    cp -- "$ratos_origin" "$ratos_archived_run/origin"
    cp -- "$ratos_state_dir/identities.json" "$ratos_archived_run/identities.json"
    chmod 0600 "$ratos_archived_run/origin" "$ratos_archived_run/identities.json"
    ratos_origin_sha=$(sha256sum -- "$ratos_archived_run/origin")
    ratos_origin_sha=${ratos_origin_sha%% *}
    ratos_identities_sha=$(sha256sum -- "$ratos_archived_run/identities.json")
    ratos_identities_sha=${ratos_identities_sha%% *}

    ratos_overlay_sha=$(sha256sum -- "$ratos_runtime_dir/$ratos_overlay_name")
    ratos_overlay_sha=${ratos_overlay_sha%% *}
    rm -f -- "$ratos_runtime_dir/$ratos_overlay_name"
    ratos_require_absent "$ratos_runtime_dir/$ratos_overlay_name"

    for ratos_runtime_file in \
        active contract-prepared.json contract-prepare-failure.json contract-succeeded contract.json \
        firstboot-required firstboot-restarted probe-succeeded probe.json
    do
        if [ -e "$ratos_runtime_dir/$ratos_runtime_file" ]; then
            mv -- "$ratos_runtime_dir/$ratos_runtime_file" \
                "$ratos_archived_run/$ratos_runtime_file"
        fi
    done

    ratos_checksums=$(mktemp "$ratos_archived_run/checksums.XXXXXX")
    for ratos_evidence_file in \
        active contract-prepared.json contract-prepare-failure.json contract-succeeded contract.json identities.json \
        firstboot-required firstboot-restarted origin probe-succeeded probe.json
    do
        if [ -e "$ratos_archived_run/$ratos_evidence_file" ]; then
            ratos_evidence_sha=$(sha256sum -- "$ratos_archived_run/$ratos_evidence_file")
            ratos_evidence_sha=${ratos_evidence_sha%% *}
            printf '%s  %s\n' "$ratos_evidence_sha" "$ratos_evidence_file" \
                >> "$ratos_checksums"
        fi
    done
    chmod 0600 "$ratos_checksums"
    mv -- "$ratos_checksums" "$ratos_archived_run/SHA256SUMS"
    ratos_checksums_sha=$(sha256sum -- "$ratos_archived_run/SHA256SUMS")
    ratos_checksums_sha=${ratos_checksums_sha%% *}
    ratos_manifest=$(mktemp "$ratos_archived_run/manifest.XXXXXX")
    printf 'outcome=%s\noverlay_retained=false\noverlay_sha256=%s\norigin_sha256=%s\nidentities_sha256=%s\nchecksums_sha256=%s\n' \
        "$ratos_run_outcome" "$ratos_overlay_sha" \
        "$ratos_origin_sha" "$ratos_identities_sha" "$ratos_checksums_sha" \
        > "$ratos_manifest"
    chmod 0600 "$ratos_manifest"
    mv -- "$ratos_manifest" "$ratos_archived_run/manifest"
}
