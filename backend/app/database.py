from .config import ADMIN_INITIAL_PASSWORD
from .auth import hash_password
from .core import db, now_ts
from .containers.ports import backfill_node_ports, enqueue_running_port_syncs

def init_schema():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                ssh_key TEXT NOT NULL DEFAULT ''
            );
            ALTER TABLE users ADD COLUMN IF NOT EXISTS external_id TEXT NOT NULL DEFAULT '';
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='users' AND column_name='student_staff_id'
                ) THEN
                    EXECUTE $sql$UPDATE users SET external_id=student_staff_id WHERE external_id='' AND student_staff_id<>''$sql$;
                    EXECUTE 'DROP INDEX IF EXISTS users_student_staff_id_idx';
                    EXECUTE 'ALTER TABLE users DROP COLUMN student_staff_id';
                END IF;
            END $$;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT NOT NULL DEFAULT '';
            ALTER TABLE users ADD COLUMN IF NOT EXISTS group_name TEXT NOT NULL DEFAULT 'member';
            ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT NOT NULL DEFAULT '';
            ALTER TABLE users ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at INTEGER NOT NULL DEFAULT 0;
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quota_profiles (
                group_name TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                cpu_cores INTEGER NOT NULL,
                memory_gb INTEGER NOT NULL,
                disk_gb INTEGER NOT NULL,
                container_disk_limit_gb INTEGER NOT NULL DEFAULT 500,
                storage_quota_gb INTEGER NOT NULL DEFAULT 500,
                gpu_count INTEGER NOT NULL,
                container_count INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quotas (
                user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                cpu_cores INTEGER NOT NULL,
                memory_gb INTEGER NOT NULL,
                disk_gb INTEGER NOT NULL,
                container_disk_limit_gb INTEGER NOT NULL DEFAULT 500,
                storage_quota_gb INTEGER NOT NULL DEFAULT 500,
                gpu_count INTEGER NOT NULL,
                container_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                pref_key TEXT NOT NULL,
                value JSONB NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, pref_key)
            );
            CREATE TABLE IF NOT EXISTS user_data_policies (
                user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                home_path TEXT NOT NULL,
                backup_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                sync_on_create BOOLEAN NOT NULL DEFAULT TRUE,
                sync_on_stop BOOLEAN NOT NULL DEFAULT FALSE,
                backup_interval_hours INTEGER NOT NULL DEFAULT 24,
                last_backup_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shared_resources (
                id BIGSERIAL PRIMARY KEY,
                resource_type TEXT NOT NULL,
                name TEXT NOT NULL,
                version TEXT NOT NULL DEFAULT 'default',
                source_path TEXT NOT NULL,
                mount_path TEXT NOT NULL,
                tags TEXT[] NOT NULL DEFAULT '{}',
                readonly BOOLEAN NOT NULL DEFAULT TRUE,
                sync_policy TEXT NOT NULL DEFAULT 'manual',
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                size_bytes BIGINT NOT NULL DEFAULT 0,
                file_count BIGINT NOT NULL DEFAULT 0,
                check_status TEXT NOT NULL DEFAULT 'unknown',
                check_error TEXT NOT NULL DEFAULT '',
                checked_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE (resource_type, name, version),
                CONSTRAINT shared_resources_type_check CHECK (resource_type IN ('dataset', 'huggingface_model', 'pytorch_model')),
                CONSTRAINT shared_resources_sync_policy_check CHECK (sync_policy IN ('manual', 'on_create', 'prewarm'))
            );
            ALTER TABLE shared_resources ADD COLUMN IF NOT EXISTS source_url TEXT NOT NULL DEFAULT '';
            ALTER TABLE shared_resources ADD COLUMN IF NOT EXISTS request_status TEXT NOT NULL DEFAULT 'ready';
            ALTER TABLE shared_resources ADD COLUMN IF NOT EXISTS requested_by BIGINT REFERENCES users(id) ON DELETE SET NULL;
            ALTER TABLE shared_resources ADD COLUMN IF NOT EXISTS upload_name TEXT NOT NULL DEFAULT '';
            ALTER TABLE shared_resources ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}';
            CREATE TABLE IF NOT EXISTS node_resource_cache (
                id BIGSERIAL PRIMARY KEY,
                node_id BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                resource_id BIGINT NOT NULL REFERENCES shared_resources(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending',
                local_path TEXT NOT NULL DEFAULT '',
                synced_at INTEGER NOT NULL DEFAULT 0,
                size_bytes BIGINT NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0,
                UNIQUE (node_id, resource_id),
                CONSTRAINT node_resource_cache_status_check CHECK (status IN ('pending', 'syncing', 'ready', 'failed'))
            );
            CREATE TABLE IF NOT EXISTS nodes (
                id BIGSERIAL PRIMARY KEY,
                hostname TEXT NOT NULL UNIQUE,
                ip TEXT NOT NULL,
                node_group TEXT NOT NULL,
                node_type TEXT NOT NULL DEFAULT 'compute',
                driver_pool TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'online',
                schedulable BOOLEAN NOT NULL DEFAULT TRUE,
                maintenance BOOLEAN NOT NULL DEFAULT FALSE,
                max_containers INTEGER NOT NULL DEFAULT 8,
                max_running_containers INTEGER NOT NULL DEFAULT 8,
                max_gpu_shared_containers INTEGER NOT NULL DEFAULT 4,
                allow_gpu_sharing BOOLEAN NOT NULL DEFAULT TRUE,
                max_cpu_per_container INTEGER NOT NULL DEFAULT 0,
                max_memory_gb_per_container INTEGER NOT NULL DEFAULT 0,
                max_disk_gb_per_container INTEGER NOT NULL DEFAULT 0,
                reserved_memory_gb INTEGER NOT NULL DEFAULT 0,
                reserved_disk_gb INTEGER NOT NULL DEFAULT 0,
                allow_port_mapping BOOLEAN NOT NULL DEFAULT TRUE,
                max_ports_per_container INTEGER NOT NULL DEFAULT 8,
                scheduler_weight INTEGER NOT NULL DEFAULT 0,
                labels JSONB NOT NULL DEFAULT '[]',
                wol_mac TEXT NOT NULL DEFAULT '',
                wol_broadcast TEXT NOT NULL DEFAULT '255.255.255.255',
                cpu_model TEXT NOT NULL DEFAULT '',
                cpu_total INTEGER NOT NULL,
                memory_total_gb INTEGER NOT NULL,
                disk_total_gb REAL NOT NULL,
                cpu_used INTEGER NOT NULL DEFAULT 0,
                memory_used_gb INTEGER NOT NULL DEFAULT 0,
                disk_used_gb REAL NOT NULL DEFAULT 0,
                last_seen INTEGER NOT NULL,
                load_avg REAL NOT NULL DEFAULT 0,
                os_version TEXT NOT NULL DEFAULT '',
                kernel_version TEXT NOT NULL DEFAULT '',
                driver_version TEXT NOT NULL DEFAULT '',
                cuda_driver_api_version TEXT NOT NULL DEFAULT '',
                incus_status TEXT NOT NULL DEFAULT 'unknown',
                agent_version TEXT NOT NULL DEFAULT '',
                uptime_seconds INTEGER NOT NULL DEFAULT 0,
                node_token TEXT NOT NULL DEFAULT '',
                registered_at INTEGER NOT NULL DEFAULT 0
            );
            ALTER TABLE nodes ADD COLUMN IF NOT EXISTS agent_update_channel TEXT NOT NULL DEFAULT 'stable';
            ALTER TABLE nodes ADD COLUMN IF NOT EXISTS agent_auto_update BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE nodes ADD COLUMN IF NOT EXISTS target_agent_version TEXT NOT NULL DEFAULT '';
            ALTER TABLE nodes ADD COLUMN IF NOT EXISTS agent_update_status TEXT NOT NULL DEFAULT 'idle';
            ALTER TABLE nodes ADD COLUMN IF NOT EXISTS agent_update_error TEXT NOT NULL DEFAULT '';
            ALTER TABLE nodes ADD COLUMN IF NOT EXISTS agent_update_at INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE nodes ADD COLUMN IF NOT EXISTS cpu_usage_percent REAL NOT NULL DEFAULT 0;
            ALTER TABLE nodes ADD COLUMN IF NOT EXISTS swap_total_gb REAL NOT NULL DEFAULT 0;
            ALTER TABLE nodes ADD COLUMN IF NOT EXISTS swap_used_gb REAL NOT NULL DEFAULT 0;
            ALTER TABLE nodes ADD COLUMN IF NOT EXISTS cpu_cores INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE nodes ADD COLUMN IF NOT EXISTS cpu_sockets INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE nodes ADD COLUMN IF NOT EXISTS cpu_temperature_c INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE nodes ADD COLUMN IF NOT EXISTS uptime_seconds INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE nodes ALTER COLUMN disk_total_gb TYPE REAL USING disk_total_gb::real;
            ALTER TABLE nodes ALTER COLUMN disk_used_gb TYPE REAL USING disk_used_gb::real;
            CREATE TABLE IF NOT EXISTS quota_profile_node_access (
                group_name TEXT NOT NULL REFERENCES quota_profiles(group_name) ON DELETE CASCADE,
                node_id BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (group_name, node_id)
            );
            CREATE TABLE IF NOT EXISTS user_node_access (
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                node_id BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, node_id)
            );
            CREATE TABLE IF NOT EXISTS user_storage_datasets (
                user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                node_id BIGINT REFERENCES nodes(id) ON DELETE SET NULL,
                dataset_name TEXT NOT NULL DEFAULT '',
                mountpoint TEXT NOT NULL,
                quota_gb INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT NOT NULL DEFAULT '',
                applied_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_releases (
                version TEXT NOT NULL,
                architecture TEXT NOT NULL DEFAULT 'amd64',
                channel TEXT NOT NULL DEFAULT 'stable',
                sha256 TEXT NOT NULL,
                size_bytes BIGINT NOT NULL,
                file_name TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_by TEXT NOT NULL DEFAULT 'admin',
                created_at INTEGER NOT NULL,
                PRIMARY KEY (version, architecture)
            );
            CREATE TABLE IF NOT EXISTS node_join_tokens (
                id BIGSERIAL PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                token_preview TEXT NOT NULL,
                expected_hostname TEXT NOT NULL DEFAULT '',
                node_group TEXT NOT NULL DEFAULT 'unassigned',
                note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                node_id BIGINT REFERENCES nodes(id) ON DELETE SET NULL,
                created_by TEXT NOT NULL DEFAULT 'admin',
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                used_at INTEGER NOT NULL DEFAULT 0
            );
            ALTER TABLE node_join_tokens ADD COLUMN IF NOT EXISTS server_url TEXT NOT NULL DEFAULT '';
            ALTER TABLE agent_releases ADD COLUMN IF NOT EXISTS changelog TEXT NOT NULL DEFAULT '';
            CREATE TABLE IF NOT EXISTS storage_volume_reports (
                node_id BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                volume_name TEXT NOT NULL,
                path TEXT NOT NULL,
                exists BOOLEAN NOT NULL DEFAULT FALSE,
                total_gb INTEGER NOT NULL DEFAULT 0,
                used_gb INTEGER NOT NULL DEFAULT 0,
                free_gb INTEGER NOT NULL DEFAULT 0,
                directory_used_gb INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'unknown',
                error TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (node_id, volume_name)
            );
            CREATE TABLE IF NOT EXISTS gpus (
                id BIGSERIAL PRIMARY KEY,
                node_id BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                slot INTEGER NOT NULL,
                uuid TEXT NOT NULL UNIQUE,
                model TEXT NOT NULL,
                pci_address TEXT NOT NULL DEFAULT '',
                vram_gb INTEGER NOT NULL,
                vram_used_mb INTEGER NOT NULL DEFAULT 0,
                temperature_c INTEGER NOT NULL DEFAULT 35,
                power_w INTEGER NOT NULL DEFAULT 40,
                utilization INTEGER NOT NULL DEFAULT 0
            );
            ALTER TABLE gpus ADD COLUMN IF NOT EXISTS vram_used_mb INTEGER NOT NULL DEFAULT 0;
            CREATE TABLE IF NOT EXISTS images (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                cuda_major INTEGER NOT NULL DEFAULT 0,
                compatible_pools TEXT NOT NULL
            );
            ALTER TABLE images ADD COLUMN IF NOT EXISTS incus_ref TEXT NOT NULL DEFAULT '';
            ALTER TABLE images ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE images ADD COLUMN IF NOT EXISTS preferred BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE images ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT 'admin';
            ALTER TABLE images ADD COLUMN IF NOT EXISTS created_at INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE images ADD COLUMN IF NOT EXISTS updated_at INTEGER NOT NULL DEFAULT 0;
            CREATE TABLE IF NOT EXISTS node_incus_images (
                node_id BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                fingerprint TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                architecture TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (node_id, fingerprint)
            );
            CREATE TABLE IF NOT EXISTS storage_image_files (
                id BIGSERIAL PRIMARY KEY,
                source_node_id BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                owner_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                fingerprint TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                architecture TEXT NOT NULL DEFAULT '',
                export_dir TEXT NOT NULL,
                base_name TEXT NOT NULL,
                size_bytes BIGINT NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT NOT NULL DEFAULT '',
                exported_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE (source_node_id, fingerprint)
            );
            ALTER TABLE storage_image_files ADD COLUMN IF NOT EXISTS owner_id BIGINT REFERENCES users(id) ON DELETE SET NULL;
            CREATE TABLE IF NOT EXISTS containers (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                owner_id BIGINT NOT NULL REFERENCES users(id),
                node_id BIGINT NOT NULL REFERENCES nodes(id),
                image_id TEXT NOT NULL REFERENCES images(id),
                status TEXT NOT NULL,
                cpu_cores INTEGER NOT NULL,
                memory_gb INTEGER NOT NULL,
                disk_gb INTEGER NOT NULL,
                ssh_username TEXT NOT NULL,
                ssh_key TEXT NOT NULL,
                mounts JSONB NOT NULL DEFAULT '[]',
                ip TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            ALTER TABLE containers ADD COLUMN IF NOT EXISTS access_status TEXT NOT NULL DEFAULT 'ready';
            ALTER TABLE containers ADD COLUMN IF NOT EXISTS access_error TEXT NOT NULL DEFAULT '';
            CREATE TABLE IF NOT EXISTS container_gpus (
                container_id BIGINT NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
                gpu_id BIGINT NOT NULL REFERENCES gpus(id),
                PRIMARY KEY (container_id, gpu_id)
            );
            CREATE TABLE IF NOT EXISTS container_ports (
                id BIGSERIAL PRIMARY KEY,
                container_id BIGINT NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
                name TEXT NOT NULL DEFAULT '',
                protocol TEXT NOT NULL,
                container_port INTEGER NOT NULL,
                host_port INTEGER NOT NULL UNIQUE,
                node_port INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                CONSTRAINT container_ports_protocol_check CHECK (protocol IN ('tcp', 'udp')),
                CONSTRAINT container_ports_container_port_check CHECK (container_port BETWEEN 1 AND 65535),
                CONSTRAINT container_ports_host_port_check CHECK (host_port BETWEEN 1 AND 65535)
            );
            CREATE TABLE IF NOT EXISTS container_resources (
                container_id BIGINT NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
                resource_id BIGINT NOT NULL REFERENCES shared_resources(id) ON DELETE CASCADE,
                mount_path TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (container_id, resource_id)
            );
            CREATE TABLE IF NOT EXISTS user_workspace_volumes (
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                node_id BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                volume_name TEXT NOT NULL,
                quota_gb INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                last_error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                removed_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, node_id)
            );
            INSERT INTO user_workspace_volumes (
                user_id, node_id, volume_name, quota_gb, status, last_error, created_at, updated_at, removed_at
            )
            SELECT DISTINCT c.owner_id, c.node_id, 'user-' || c.owner_id || '-ws', 0, 'active', '', 0, 0, 0
            FROM containers c
            ON CONFLICT (user_id, node_id) DO NOTHING;
            CREATE TABLE IF NOT EXISTS container_sync_rules (
                id BIGSERIAL PRIMARY KEY,
                container_id BIGINT NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
                rule_type TEXT NOT NULL,
                direction TEXT NOT NULL DEFAULT 'container_to_storage',
                name TEXT NOT NULL DEFAULT '',
                container_path TEXT NOT NULL,
                storage_relative_path TEXT NOT NULL DEFAULT '',
                resource_id BIGINT REFERENCES shared_resources(id) ON DELETE CASCADE,
                interval_minutes INTEGER NOT NULL DEFAULT 60,
                schedule_kind TEXT NOT NULL DEFAULT 'daily',
                schedule_time_seconds INTEGER NOT NULL DEFAULT 0,
                conflict_policy TEXT NOT NULL DEFAULT 'overwrite',
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                last_run_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                CONSTRAINT container_sync_rules_type_check CHECK (rule_type IN ('scheduled_upload', 'realtime_sync', 'resource_pull')),
                CONSTRAINT container_sync_rules_direction_check CHECK (direction IN ('container_to_storage', 'storage_to_container')),
                CONSTRAINT container_sync_rules_interval_check CHECK (interval_minutes BETWEEN 1 AND 43200),
                CONSTRAINT container_sync_rules_conflict_policy_check CHECK (conflict_policy IN ('overwrite', 'skip'))
            );
            CREATE INDEX IF NOT EXISTS container_sync_rules_schedule_idx
                ON container_sync_rules (enabled, rule_type, last_run_at);
            CREATE TABLE IF NOT EXISTS node_tasks (
                id BIGSERIAL PRIMARY KEY,
                node_id BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                container_id BIGINT REFERENCES containers(id) ON DELETE CASCADE,
                task_type TEXT NOT NULL,
                payload JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                claimed_at INTEGER NOT NULL DEFAULT 0,
                finished_at INTEGER NOT NULL DEFAULT 0,
                available_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            );
            ALTER TABLE node_tasks ADD COLUMN IF NOT EXISTS result JSONB NOT NULL DEFAULT '{}';
            CREATE TABLE IF NOT EXISTS data_sync_tasks (
                id BIGSERIAL PRIMARY KEY,
                task_type TEXT NOT NULL,
                user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                resource_id BIGINT REFERENCES shared_resources(id) ON DELETE SET NULL,
                source_node_id BIGINT REFERENCES nodes(id) ON DELETE SET NULL,
                target_node_id BIGINT REFERENCES nodes(id) ON DELETE SET NULL,
                container_id BIGINT REFERENCES containers(id) ON DELETE SET NULL,
                source_path TEXT NOT NULL DEFAULT '',
                target_path TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'planned',
                detail JSONB NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                finished_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS user_directory_scans (
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL DEFAULT '',
                node_id BIGINT REFERENCES nodes(id) ON DELETE SET NULL,
                absolute_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',
                file_count BIGINT NOT NULL DEFAULT 0,
                size_bytes BIGINT NOT NULL DEFAULT 0,
                entries JSONB NOT NULL DEFAULT '[]',
                truncated BOOLEAN NOT NULL DEFAULT FALSE,
                error TEXT NOT NULL DEFAULT '',
                scanned_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, relative_path)
            );
            CREATE TABLE IF NOT EXISTS shared_resource_scans (
                resource_id BIGINT NOT NULL REFERENCES shared_resources(id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL DEFAULT '',
                node_id BIGINT REFERENCES nodes(id) ON DELETE SET NULL,
                absolute_path TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unknown',
                file_count BIGINT NOT NULL DEFAULT 0,
                size_bytes BIGINT NOT NULL DEFAULT 0,
                entries JSONB NOT NULL DEFAULT '[]',
                truncated BOOLEAN NOT NULL DEFAULT FALSE,
                error TEXT NOT NULL DEFAULT '',
                scanned_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (resource_id, relative_path)
            );
            CREATE TABLE IF NOT EXISTS node_cache_inventory (
                resource_id BIGINT NOT NULL REFERENCES shared_resources(id) ON DELETE CASCADE,
                node_id BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',
                size_bytes BIGINT NOT NULL DEFAULT 0,
                file_count BIGINT NOT NULL DEFAULT 0,
                verification TEXT NOT NULL DEFAULT 'size',
                digest TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                last_synced_at INTEGER NOT NULL DEFAULT 0,
                last_verified_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (resource_id, node_id)
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id BIGSERIAL PRIMARY KEY,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                detail JSONB NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute("ALTER TABLE gpus ADD COLUMN IF NOT EXISTS pci_address TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE container_ports ADD COLUMN IF NOT EXISTS node_port INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS node_type TEXT NOT NULL DEFAULT 'compute'")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS schedulable BOOLEAN NOT NULL DEFAULT TRUE")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS max_containers INTEGER NOT NULL DEFAULT 8")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS max_running_containers INTEGER NOT NULL DEFAULT 8")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS max_gpu_shared_containers INTEGER NOT NULL DEFAULT 4")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS allow_gpu_sharing BOOLEAN NOT NULL DEFAULT TRUE")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS max_cpu_per_container INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS max_memory_gb_per_container INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS max_disk_gb_per_container INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS reserved_memory_gb INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS reserved_disk_gb INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS allow_port_mapping BOOLEAN NOT NULL DEFAULT TRUE")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS max_ports_per_container INTEGER NOT NULL DEFAULT 8")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS scheduler_weight INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS labels JSONB NOT NULL DEFAULT '[]'")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS wol_mac TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS wol_broadcast TEXT NOT NULL DEFAULT '255.255.255.255'")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS cpu_model TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE containers ADD COLUMN IF NOT EXISTS sync_mode TEXT NOT NULL DEFAULT 'on_demand'")
        conn.execute("ALTER TABLE containers ADD COLUMN IF NOT EXISTS sync_direction TEXT NOT NULL DEFAULT 'storage_to_container'")
        conn.execute("ALTER TABLE containers ADD COLUMN IF NOT EXISTS sync_interval_minutes INTEGER NOT NULL DEFAULT 60")
        conn.execute("ALTER TABLE containers ADD COLUMN IF NOT EXISTS sync_incremental BOOLEAN NOT NULL DEFAULT TRUE")
        conn.execute("ALTER TABLE container_sync_rules ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'container_to_storage'")
        conn.execute("ALTER TABLE container_sync_rules ADD COLUMN IF NOT EXISTS schedule_kind TEXT NOT NULL DEFAULT 'daily'")
        conn.execute("ALTER TABLE container_sync_rules ADD COLUMN IF NOT EXISTS schedule_time_seconds INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE container_sync_rules ADD COLUMN IF NOT EXISTS conflict_policy TEXT NOT NULL DEFAULT 'overwrite'")
        conn.execute("ALTER TABLE container_sync_rules DROP CONSTRAINT IF EXISTS container_sync_rules_conflict_policy_check")
        conn.execute("ALTER TABLE container_sync_rules ADD CONSTRAINT container_sync_rules_conflict_policy_check CHECK (conflict_policy IN ('overwrite', 'skip'))")
        conn.execute("ALTER TABLE container_sync_rules DROP CONSTRAINT IF EXISTS container_sync_rules_type_check")
        conn.execute(
            """INSERT INTO container_sync_rules (container_id,rule_type,direction,name,container_path,storage_relative_path,resource_id,interval_minutes,enabled,last_run_at,created_at,updated_at)
               SELECT container_id,'realtime_sync','storage_to_container',name || '（存储到容器）',container_path,storage_relative_path,resource_id,interval_minutes,enabled,last_run_at,created_at,updated_at
               FROM container_sync_rules WHERE rule_type='realtime_bidirectional'"""
        )
        conn.execute("UPDATE container_sync_rules SET rule_type='realtime_sync',direction='container_to_storage',name=name || '（容器到存储）' WHERE rule_type='realtime_bidirectional'")
        conn.execute("ALTER TABLE container_sync_rules ADD CONSTRAINT container_sync_rules_type_check CHECK (rule_type IN ('scheduled_upload', 'realtime_sync', 'resource_pull'))")
        conn.execute("ALTER TABLE container_sync_rules DROP CONSTRAINT IF EXISTS container_sync_rules_direction_check")
        conn.execute("ALTER TABLE container_sync_rules ADD CONSTRAINT container_sync_rules_direction_check CHECK (direction IN ('container_to_storage', 'storage_to_container'))")
        conn.execute("ALTER TABLE node_tasks ADD COLUMN IF NOT EXISTS available_at INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS node_tasks_claim_idx ON node_tasks (node_id, status, available_at, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS data_sync_tasks_container_idx ON data_sync_tasks (container_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS data_sync_tasks_status_idx ON data_sync_tasks (status, updated_at DESC) WHERE status IN ('planned','running','verifying')"
        )
        conn.execute("ALTER TABLE node_tasks ADD COLUMN IF NOT EXISTS data_sync_task_id BIGINT REFERENCES data_sync_tasks(id) ON DELETE SET NULL")
        conn.execute("ALTER TABLE data_sync_tasks ADD COLUMN IF NOT EXISTS progress JSONB NOT NULL DEFAULT '{}'")
        conn.execute("ALTER TABLE shared_resources DROP CONSTRAINT IF EXISTS shared_resources_type_check")
        conn.execute(
            "ALTER TABLE shared_resources ADD CONSTRAINT shared_resources_type_check CHECK (resource_type IN ('dataset', 'huggingface_model', 'pytorch_model'))"
        )
        conn.execute("ALTER TABLE shared_resources ADD COLUMN IF NOT EXISTS version TEXT NOT NULL DEFAULT 'default'")
        conn.execute("ALTER TABLE shared_resources ADD COLUMN IF NOT EXISTS size_bytes BIGINT NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE shared_resources ADD COLUMN IF NOT EXISTS file_count BIGINT NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE shared_resources ADD COLUMN IF NOT EXISTS check_status TEXT NOT NULL DEFAULT 'unknown'")
        conn.execute("ALTER TABLE shared_resources ADD COLUMN IF NOT EXISTS check_error TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE shared_resources ADD COLUMN IF NOT EXISTS checked_at INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE shared_resources ADD COLUMN IF NOT EXISTS download_progress JSONB NOT NULL DEFAULT '{}'")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS ssh_user TEXT NOT NULL DEFAULT 'root'")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS ssh_port INTEGER NOT NULL DEFAULT 22")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS sync_ip TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS sync_ssh_port INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS resource_cache_base TEXT NOT NULL DEFAULT ''")
        # 将已有公开资源的 mount_path 标准化为 /datasets/{name} 和 /models/{name}
        conn.execute("""
            UPDATE shared_resources
            SET mount_path = '/datasets/' || name
            WHERE resource_type = 'dataset'
              AND mount_path NOT LIKE '/datasets/%'
        """)
        conn.execute("""
            UPDATE shared_resources
            SET mount_path = '/models/' || name
            WHERE resource_type != 'dataset'
              AND mount_path NOT LIKE '/models/%'
        """)
        conn.execute("ALTER TABLE shared_resources DROP CONSTRAINT IF EXISTS shared_resources_resource_type_name_key")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS shared_resources_resource_version_idx ON shared_resources (resource_type, name, version)"
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_external_id_idx ON users (external_id) WHERE external_id != ''")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_idx ON users (LOWER(email)) WHERE email != ''")
        # SSO 统一认证：外部身份绑定表
        conn.execute(
            """CREATE TABLE IF NOT EXISTS user_identities (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                subject TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE (provider, subject)
            )"""
        )
        # SSO CSRF 防护：临时状态表（10 分钟 TTL，由 sso/routes.py 原子删除）
        conn.execute(
            """CREATE TABLE IF NOT EXISTS sso_states (
                state TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS resource_tag_options (
                tag TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL DEFAULT 0
            )"""
        )
        # 预填充内置标签库
        builtin_tags = [
            "音频/语音识别","音频/语音合成","音频/音频分类",
            "视频/目标追踪","视频/视频生成","视频/动作识别",
            "多模态/图像描述","多模态/文本生成视频","多模态/视觉问答",
            "图像/分类","图像/生成",
            "文本/大语言模型","文本/信息抽取",
        ]
        ts_now = now_ts()
        for t in builtin_tags:
            conn.execute(
                "INSERT INTO resource_tag_options (tag, created_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (t, ts_now),
            )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS resource_tag_options (
                tag TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL DEFAULT 0
            )"""
        )
        builtin_tags = [
            "音频/语音识别","音频/语音合成","音频/音频分类",
            "视频/目标追踪","视频/视频生成","视频/动作识别",
            "多模态/图像描述","多模态/文本生成视频","多模态/视觉问答",
            "图像/分类","图像/生成",
            "文本/大语言模型","文本/信息抽取",
        ]
        ts_now = now_ts()
        for _t in builtin_tags:
            conn.execute(
                "INSERT INTO resource_tag_options (tag, created_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (_t, ts_now),
            )
                # 项目/组能力已从当前产品阶段移除；清理旧版本遗留结构和数据。
        conn.execute("DROP TABLE IF EXISTS container_projects")
        conn.execute("ALTER TABLE containers DROP COLUMN IF EXISTS data_profile")
        conn.execute("ALTER TABLE data_sync_tasks DROP COLUMN IF EXISTS project_id")
        conn.execute("ALTER TABLE data_sync_tasks DROP COLUMN IF EXISTS policy_id")
        conn.execute("DROP TABLE IF EXISTS sync_policy_targets")
        conn.execute("DROP TABLE IF EXISTS sync_policies")
        conn.execute("DROP TABLE IF EXISTS project_storage_policies")
        conn.execute("DROP TABLE IF EXISTS project_members")
        conn.execute("DROP TABLE IF EXISTS projects")
        conn.execute("DELETE FROM storage_volume_reports WHERE volume_name = 'projects'")
        # 联系电话字段
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE quota_profiles ADD COLUMN IF NOT EXISTS container_disk_limit_gb INTEGER NOT NULL DEFAULT 500")
        conn.execute("ALTER TABLE quota_profiles ADD COLUMN IF NOT EXISTS storage_quota_gb INTEGER NOT NULL DEFAULT 500")
        conn.execute("ALTER TABLE quotas ADD COLUMN IF NOT EXISTS container_disk_limit_gb INTEGER NOT NULL DEFAULT 500")
        conn.execute("ALTER TABLE quotas ADD COLUMN IF NOT EXISTS storage_quota_gb INTEGER NOT NULL DEFAULT 500")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS user_storage_datasets (
                user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                node_id BIGINT REFERENCES nodes(id) ON DELETE SET NULL,
                dataset_name TEXT NOT NULL DEFAULT '',
                mountpoint TEXT NOT NULL,
                quota_gb INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT NOT NULL DEFAULT '',
                applied_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )"""
        )
        # 多 SSH 公钥表（支持有效期；expires_at=0 表示永久有效）
        conn.execute(
            """CREATE TABLE IF NOT EXISTS user_ssh_keys (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                label TEXT NOT NULL DEFAULT '',
                public_key TEXT NOT NULL,
                expires_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )"""
        )
        # 将 users.ssh_key 中已有的公钥迁移到 user_ssh_keys（每用户只迁移一次）
        conn.execute(
            """
            INSERT INTO user_ssh_keys (user_id, label, public_key, expires_at, created_at)
            SELECT id, '默认公钥', ssh_key, 0, created_at
            FROM users
            WHERE ssh_key != ''
              AND NOT EXISTS (SELECT 1 FROM user_ssh_keys k WHERE k.user_id = users.id)
            """
        )
        backfill_node_ports(conn)
        seed_defaults(conn)
        enqueue_running_port_syncs(conn)
        # API Token 表
        conn.execute(
            """CREATE TABLE IF NOT EXISTS api_tokens (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL DEFAULT '',
                token_hash TEXT NOT NULL UNIQUE,
                token_preview TEXT NOT NULL,
                expires_at INTEGER NOT NULL DEFAULT 0,
                last_used_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )"""
        )
        # 容器到期时间
        conn.execute("ALTER TABLE containers ADD COLUMN IF NOT EXISTS expires_at INTEGER NOT NULL DEFAULT 0")
        # 节点指标历史（轻量监控）
        conn.execute(
            """CREATE TABLE IF NOT EXISTS node_metrics_snapshots (
                id BIGSERIAL PRIMARY KEY,
                node_id BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                sampled_at INTEGER NOT NULL,
                cpu_pct REAL NOT NULL DEFAULT 0,
                memory_pct REAL NOT NULL DEFAULT 0,
                disk_pct REAL NOT NULL DEFAULT 0,
                gpu_avg_pct REAL NOT NULL DEFAULT 0,
                gpu_avg_vram_pct REAL NOT NULL DEFAULT 0,
                temperature_c INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS node_metrics_snapshots_node_time_idx "
            "ON node_metrics_snapshots (node_id, sampled_at DESC)"
        )

def migrate_group_names(conn, ts: int):
    legacy_groups = conn.execute(
        """
        SELECT 1
        WHERE EXISTS (SELECT 1 FROM users WHERE group_name IN ('teacher', 'student'))
           OR EXISTS (SELECT 1 FROM quota_profiles WHERE group_name IN ('teacher', 'student'))
           OR EXISTS (
                SELECT 1 FROM users
                WHERE username='admin' AND group_name='admin'
                  AND NOT EXISTS (SELECT 1 FROM quota_profiles WHERE group_name='platform_admin')
           )
        """
    ).fetchone()
    if not legacy_groups:
        return

    legacy_admin_group = conn.execute(
        """
        SELECT 1
        WHERE NOT EXISTS (SELECT 1 FROM quota_profiles WHERE group_name='platform_admin')
          AND (
              EXISTS (SELECT 1 FROM quota_profiles WHERE group_name='admin')
              OR EXISTS (SELECT 1 FROM users WHERE username='admin' AND group_name='admin')
          )
        """
    ).fetchone()
    if legacy_admin_group:
        conn.execute(
            """
            INSERT INTO quota_profiles (
                group_name, role, cpu_cores, memory_gb, disk_gb,
                container_disk_limit_gb, storage_quota_gb, gpu_count, container_count, updated_at
            )
            SELECT 'platform_admin', 'admin', cpu_cores, memory_gb, disk_gb,
                   container_disk_limit_gb, storage_quota_gb, gpu_count, container_count, %s
            FROM quota_profiles WHERE group_name='admin'
            ON CONFLICT (group_name) DO NOTHING
            """,
            (ts,),
        )
        conn.execute(
            """
            INSERT INTO quota_profile_node_access (group_name, node_id, created_at)
            SELECT 'platform_admin', node_id, created_at
            FROM quota_profile_node_access
            WHERE group_name='admin'
            ON CONFLICT DO NOTHING
            """
        )
        conn.execute("UPDATE users SET group_name='platform_admin', role='admin' WHERE group_name='admin'")
        conn.execute("DELETE FROM quota_profiles WHERE group_name='admin' AND EXISTS (SELECT 1 FROM quota_profiles WHERE group_name='platform_admin')")

    conn.execute(
        """
        INSERT INTO quota_profiles (
            group_name, role, cpu_cores, memory_gb, disk_gb,
            container_disk_limit_gb, storage_quota_gb, gpu_count, container_count, updated_at
        )
        SELECT 'admin', 'admin', cpu_cores, memory_gb, disk_gb,
               container_disk_limit_gb, storage_quota_gb, gpu_count, container_count, %s
        FROM quota_profiles WHERE group_name='teacher'
        ON CONFLICT (group_name) DO NOTHING
        """,
        (ts,),
    )
    conn.execute(
        """
        INSERT INTO quota_profile_node_access (group_name, node_id, created_at)
        SELECT 'admin', node_id, created_at
        FROM quota_profile_node_access
        WHERE group_name='teacher'
        ON CONFLICT DO NOTHING
        """
    )
    conn.execute("UPDATE users SET group_name='admin', role='admin' WHERE group_name='teacher'")
    conn.execute("DELETE FROM quota_profiles WHERE group_name='teacher'")

    conn.execute(
        """
        INSERT INTO quota_profiles (
            group_name, role, cpu_cores, memory_gb, disk_gb,
            container_disk_limit_gb, storage_quota_gb, gpu_count, container_count, updated_at
        )
        SELECT 'member', 'member', cpu_cores, memory_gb, disk_gb,
               container_disk_limit_gb, storage_quota_gb, gpu_count, container_count, %s
        FROM quota_profiles WHERE group_name='student'
        ON CONFLICT (group_name) DO NOTHING
        """,
        (ts,),
    )
    conn.execute(
        """
        INSERT INTO quota_profile_node_access (group_name, node_id, created_at)
        SELECT 'member', node_id, created_at
        FROM quota_profile_node_access
        WHERE group_name='student'
        ON CONFLICT DO NOTHING
        """
    )
    conn.execute("UPDATE users SET group_name='member', role='member' WHERE group_name='student'")
    conn.execute("DELETE FROM quota_profiles WHERE group_name='student'")

    conn.execute(
        """
        UPDATE system_settings
        SET value = CASE value
            WHEN 'admin' THEN 'platform_admin'
            WHEN 'teacher' THEN 'admin'
            WHEN 'student' THEN 'member'
            ELSE value
        END
        WHERE key IN ('platform_registration_default_group', 'sso_default_group')
        """
    )

def seed_defaults(conn):
    if not conn.execute("SELECT 1 FROM users WHERE username = 'admin'").fetchone():
        user_id = conn.execute(
            """
            INSERT INTO users (username, display_name, role, ssh_key)
            VALUES ('admin', '平台管理员', 'admin', '')
            RETURNING id
            """
        ).fetchone()["id"]
        conn.execute(
            """INSERT INTO quotas (
                user_id, cpu_cores, memory_gb, disk_gb,
                container_disk_limit_gb, storage_quota_gb, gpu_count, container_count
            ) VALUES (%s, 32, 128, 1000, 500, 1000, 4, 4)""",
            (user_id,),
        )
    ts = now_ts()
    migrate_group_names(conn, ts)
    admin = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    if admin:
        conn.execute(
            "UPDATE users SET role='admin', group_name='platform_admin', enabled=TRUE, "
            "password_hash=CASE WHEN password_hash='' THEN %s ELSE password_hash END, "
            "created_at=CASE WHEN created_at=0 THEN %s ELSE created_at END WHERE id=%s",
            (hash_password(ADMIN_INITIAL_PASSWORD), ts, admin["id"]),
        )
    for profile in [
        ("platform_admin", "admin", 128, 512, 4096, 500, 4096, 16, 16),
        ("admin", "admin", 32, 128, 1000, 500, 1000, 4, 4),
        ("member", "member", 16, 64, 500, 500, 500, 1, 2),
        ("guest", "member", 4, 16, 100, 100, 100, 0, 1),
    ]:
        conn.execute(
            """INSERT INTO quota_profiles (
                group_name, role, cpu_cores, memory_gb, disk_gb,
                container_disk_limit_gb, storage_quota_gb, gpu_count, container_count, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (group_name) DO NOTHING""",
            (*profile, ts),
        )
    conn.execute("UPDATE quota_profiles SET role='admin', updated_at=%s WHERE group_name IN ('platform_admin', 'admin') AND role!='admin'", (ts,))
    conn.execute("UPDATE users SET role='admin' WHERE group_name IN ('platform_admin', 'admin') AND role!='admin'")
    if admin and not conn.execute("SELECT 1 FROM user_data_policies WHERE user_id = %s", (admin["id"],)).fetchone():
        conn.execute(
            """
            INSERT INTO user_data_policies (
                user_id, home_path, backup_enabled, sync_on_create, sync_on_stop,
                backup_interval_hours, created_at, updated_at
            ) VALUES (%s, '/data/users/admin', TRUE, TRUE, FALSE, 24, %s, %s)
            """,
            (admin["id"], ts, ts),
        )
