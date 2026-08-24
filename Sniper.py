import oci
import os
import time
import random
import logging
import subprocess
from datetime import datetime

# --- LOAD .env FILE (for local runs) ---
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

# --- CONFIGURATION ---
# Values loaded from environment variables (set via GitHub Secrets or .env locally)
COMPARTMENT_ID = os.environ.get("OCI_COMPARTMENT_ID", "")
AD_NAME = os.environ.get("OCI_AD_NAME", "")
IMAGE_ID = os.environ.get("OCI_IMAGE_ID", "")
SUBNET_ID = os.environ.get("OCI_SUBNET_ID", "")
# Optional: launch from an existing boot volume instead of a fresh image, which
# restores the old instance's disk (data, packages, SSH keys) as-is. Takes
# precedence over IMAGE_ID when set. Must live in the same AD as AD_NAME.
BOOT_VOLUME_ID = os.environ.get("OCI_BOOT_VOLUME_ID", "")
SSH_PUB_KEY = os.environ.get("OCI_SSH_PUB_KEY", "")

# --- SHAPE CONFIGS TO TRY (from largest to smallest) ---
# Oracle cut the Always Free A1 entitlement to 2 OCPUs / 12 GB total, enforced
# from 2026-08-18. Anything above that is rejected (or auto-terminated later),
# so 2 OCPU / 12 GB is now the full-size target.
SHAPE_CONFIGS = [
    {"ocpus": 2, "memory_in_gbs": 12, "name": "2 OCPU / 12 GB"},   # full free-tier allowance
    {"ocpus": 1, "memory_in_gbs": 6,  "name": "1 OCPU / 6 GB"},    # fallback: half size
]

# Set to True to try smaller shapes if 2/12 keeps failing
TRY_SMALLER_SHAPES = True

# After this many capacity failures on the full size, start interleaving the
# smaller shapes. Capacity windows last seconds, so once we know 2/12 is scarce
# it is better to alternate full/half every other attempt than to give up on
# either one - a 1 OCPU foothold in the region beats nothing.
#
# Was 20, which in practice meant the fallback never fired: capacity_failures
# resets to 0 on every process start, and local runs are cut short whenever the
# PC sleeps. Across 241 logged attempts, every single one asked for 2/12 and the
# half-size shape was never tried once. 6 is reached inside ~10 minutes, so the
# fallback now actually engages in a short run.
SMALLER_SHAPE_AFTER = 6

# --- FAULT DOMAIN ROTATION ---
# Mumbai is a single-AD region, but each AD has 3 fault domains and A1 capacity
# is often stranded in just one of them. Leaving fault_domain unset lets Oracle
# choose; rotating explicitly also probes FDs its placement logic skips. None
# keeps the original "let Oracle pick" behaviour in the rotation.
ROTATE_FAULT_DOMAINS = True
FAULT_DOMAINS = [None, "FAULT-DOMAIN-1", "FAULT-DOMAIN-2", "FAULT-DOMAIN-3"]

# --- RETRY TIMING ---
# Tuned from run 32226068192: at a 30-55s cadence, 45% of attempts came back
# HTTP 429 and the resulting backoff ate 76% of the run. A slower, steadier
# cadence stays under the throttle and yields more real attempts per hour.
CAPACITY_RETRY_MIN_SECONDS = 90
CAPACITY_RETRY_MAX_SECONDS = 140
RATE_LIMIT_FIRST_MIN_SECONDS = 180
RATE_LIMIT_FIRST_MAX_SECONDS = 300
# Capping lower than the old 900s: long backoffs create blind spots where
# freed capacity gets taken by someone else.
RATE_LIMIT_MAX_SECONDS = 420
NETWORK_RETRY_MIN_SECONDS = 30
NETWORK_RETRY_MAX_SECONDS = 90

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("sniper.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("sniper")

# --- OCI CLIENT SETUP ---
config = oci.config.from_file()
config["region"] = "ap-mumbai-1"
compute_client = oci.core.ComputeClient(config)
network_client = oci.core.VirtualNetworkClient(config)

# --- STATS ---
attempt_count = 0
start_time = datetime.now()


def get_retry_after_seconds(error, consecutive_rate_limits):
    headers = getattr(error, "headers", {}) or {}
    retry_after = headers.get("retry-after") or headers.get("Retry-After")

    if retry_after:
        try:
            return max(int(retry_after), RATE_LIMIT_FIRST_MIN_SECONDS)
        except ValueError:
            pass

    min_seconds = min(
        RATE_LIMIT_FIRST_MIN_SECONDS * (2 ** max(consecutive_rate_limits - 1, 0)),
        RATE_LIMIT_MAX_SECONDS,
    )
    max_seconds = min(
        RATE_LIMIT_FIRST_MAX_SECONDS * (2 ** max(consecutive_rate_limits - 1, 0)),
        RATE_LIMIT_MAX_SECONDS,
    )
    return random.randint(min_seconds, max_seconds)


def wait_for_public_ip(instance_id, timeout_seconds=600):
    """Poll until the instance's VNIC is attached, then return its public IP."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            attachments = compute_client.list_vnic_attachments(
                compartment_id=COMPARTMENT_ID,
                instance_id=instance_id,
            ).data
            for attachment in attachments:
                if attachment.lifecycle_state == "ATTACHED" and attachment.vnic_id:
                    vnic = network_client.get_vnic(attachment.vnic_id).data
                    if vnic.public_ip:
                        return vnic.public_ip
        except Exception as e:  # never let an IP lookup failure lose us the instance
            log.warning("Public IP lookup failed, retrying: %s", e)
        time.sleep(10)
    return None


def try_launch(shape_config, consecutive_rate_limits, fault_domain=None):
    """Attempt to launch an instance with the given shape config."""
    global attempt_count
    attempt_count += 1

    try:
        vnic_specs = oci.core.models.CreateVnicDetails(
            subnet_id=SUBNET_ID,
            assign_public_ip=True,
            display_name="AI_VNIC",
        )

        shape_specs = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=shape_config["ocpus"],
            memory_in_gbs=shape_config["memory_in_gbs"],
        )

        if BOOT_VOLUME_ID:
            source_specs = oci.core.models.InstanceSourceViaBootVolumeDetails(
                boot_volume_id=BOOT_VOLUME_ID,
            )
        else:
            source_specs = oci.core.models.InstanceSourceViaImageDetails(
                image_id=IMAGE_ID,
            )

        request = oci.core.models.LaunchInstanceDetails(
            compartment_id=COMPARTMENT_ID,
            availability_domain=AD_NAME,
            shape="VM.Standard.A1.Flex",
            shape_config=shape_specs,
            source_details=source_specs,
            create_vnic_details=vnic_specs,
            display_name="AI_Brain_12GB",
            metadata={"ssh_authorized_keys": SSH_PUB_KEY},
        )

        if fault_domain:
            request.fault_domain = fault_domain

        elapsed = datetime.now() - start_time
        log.info(
            "Attempt #%d [%s/%s] | Running for %s | Trying %s ...",
            attempt_count,
            AD_NAME.split(":")[-1],
            fault_domain.replace("FAULT-DOMAIN-", "FD") if fault_domain else "FD-any",
            str(elapsed).split(".")[0],
            shape_config["name"],
        )

        response = compute_client.launch_instance(request)
        instance = response.data

        log.info("=" * 60)
        log.info("SUCCESS! Instance is launching!")
        log.info("Instance ID : %s", instance.id)
        log.info("Shape       : %s (%s)", instance.shape, shape_config["name"])
        log.info("State       : %s", instance.lifecycle_state)
        log.info("After %d attempts over %s", attempt_count, str(elapsed).split(".")[0])
        log.info("=" * 60)

        log.info("Waiting for the public IP to be assigned...")
        public_ip = wait_for_public_ip(instance.id)
        if public_ip:
            log.info("Public IP  : %s", public_ip)
            log.info("Login with : ssh -i <your-private-key> ubuntu@%s", public_ip)
        else:
            log.warning("Public IP not assigned yet - check the OCI console.")

        # Write success details to a file for easy reference
        with open("instance_success.txt", "w") as f:
            f.write(f"Instance ID: {instance.id}\n")
            f.write(f"Shape: {instance.shape} ({shape_config['name']})\n")
            f.write(f"Time: {datetime.now()}\n")
            f.write(f"Public IP: {public_ip or 'pending - check OCI console'}\n")
            f.write(f"Source: {'boot volume ' + BOOT_VOLUME_ID if BOOT_VOLUME_ID else 'image ' + IMAGE_ID}\n")
            f.write(f"Attempts: {attempt_count}\n")

        # If running on GitHub Actions, disable the workflow so it stops
        if os.environ.get("GITHUB_ACTIONS"):
            log.info("Running on GitHub Actions - disabling workflow to stop future runs...")
            repo = os.environ.get("GITHUB_REPOSITORY", "")
            token = os.environ.get("GITHUB_TOKEN", "")
            if repo and token:
                subprocess.run(
                    ["gh", "workflow", "disable", "sniper.yml"],
                    env={**os.environ, "GH_TOKEN": token},
                    check=False,
                )

        return True, None, 0

    except oci.exceptions.ServiceError as e:
        if e.status == 500 or "Out of host capacity" in str(e):
            retry_after = random.randint(CAPACITY_RETRY_MIN_SECONDS, CAPACITY_RETRY_MAX_SECONDS)
            log.info(
                "Attempt #%d | No capacity for %s. Next try in %ds...",
                attempt_count,
                shape_config["name"],
                retry_after,
            )
            return False, retry_after, 0

        elif e.status == 429:
            consecutive_rate_limits += 1
            retry_after = get_retry_after_seconds(e, consecutive_rate_limits)
            log.warning(
                "Rate limited (#%d). Backing off for %ds (%.1f min)...",
                consecutive_rate_limits,
                retry_after,
                retry_after / 60,
            )
            return False, retry_after, consecutive_rate_limits

        elif e.status == 404:
            log.error("404 Error - resource not found. Check BOOT_VOLUME_ID/IMAGE_ID, SUBNET_ID, COMPARTMENT_ID, AD_NAME.")
            log.error("Details: %s", e.message)
            return True, None, consecutive_rate_limits

        elif e.status == 400 and "LimitExceeded" in str(e):
            log.error("Limit exceeded - free tier is 2 OCPU / 12 GB total; you likely already have an A1 instance using it.")
            log.error("Details: %s", e.message)
            return True, None, consecutive_rate_limits

        else:
            log.error("Unexpected OCI error (status %d): %s", e.status, e.message)
            retry_after = random.randint(NETWORK_RETRY_MIN_SECONDS, NETWORK_RETRY_MAX_SECONDS)
            return False, retry_after, consecutive_rate_limits

    except oci.exceptions.RequestException as e:
        retry_after = random.randint(NETWORK_RETRY_MIN_SECONDS, NETWORK_RETRY_MAX_SECONDS)
        log.warning("Network error. Retrying in %ds... (%s)", retry_after, e)
        return False, retry_after, consecutive_rate_limits


def run_sniper():
    consecutive_rate_limits = 0
    capacity_failures = 0

    log.info("Oracle ARM Instance Sniper started")
    log.info("Target: VM.Standard.A1.Flex in %s", AD_NAME)
    if BOOT_VOLUME_ID:
        log.info("Source: existing boot volume %s", BOOT_VOLUME_ID)
    else:
        log.info("Source: image %s", IMAGE_ID)
    log.info("Retry interval: %d-%ds | Smaller shape fallback: %s | FD rotation: %s",
             CAPACITY_RETRY_MIN_SECONDS, CAPACITY_RETRY_MAX_SECONDS,
             ("ON (after %d capacity failures)" % SMALLER_SHAPE_AFTER)
             if TRY_SMALLER_SHAPES else "OFF",
             "ON" if ROTATE_FAULT_DOMAINS else "OFF")
    log.info("-" * 60)

    while True:
        # Pick which shape config to try. Once the full size has proven scarce,
        # alternate full/half so neither is starved - both windows are brief.
        if TRY_SMALLER_SHAPES and capacity_failures >= SMALLER_SHAPE_AFTER:
            shape = SHAPE_CONFIGS[attempt_count % len(SHAPE_CONFIGS)]
        else:
            shape = SHAPE_CONFIGS[0]  # Always try full 2/12 first

        # Rotate fault domains so stranded capacity in one FD still gets probed.
        if ROTATE_FAULT_DOMAINS:
            fault_domain = FAULT_DOMAINS[attempt_count % len(FAULT_DOMAINS)]
        else:
            fault_domain = None

        finished, retry_delay, consecutive_rate_limits = try_launch(
            shape, consecutive_rate_limits, fault_domain
        )

        if finished:
            break

        # Track capacity failures. A 429 says nothing about capacity, so it must
        # not reset the counter - doing so kept the fallback from ever engaging.
        if consecutive_rate_limits == 0:
            capacity_failures += 1

        time.sleep(retry_delay)


if __name__ == "__main__":
    run_sniper()
