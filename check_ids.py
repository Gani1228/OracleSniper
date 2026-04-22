import oci
config = oci.config.from_file()
core_client = oci.core.VirtualNetworkClient(config)

# Paste your Subnet ID here to test it
SUBNET_ID = "YOUR_SUBNET_OCID_HERE"

try:
    subnet = core_client.get_subnet(SUBNET_ID).data
    print(f"Success! Subnet found: {subnet.display_name}")
except Exception as e:
    print(f"Failed! The Subnet ID is wrong or inaccessible: {e}")
