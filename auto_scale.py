#!/usr/bin/env python3

import requests
import time
import os
import subprocess
import json
import psutil
import logging
from google.cloud import compute_v1
from google.oauth2 import service_account

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("auto_scale.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("auto_scale")

# Configuration
CPU_THRESHOLD = 75 #Percentage
MEM_THRESHOLD = 75  # Percentage
DISK_THRESHOLD = 75  # Percentage
GCP_SERVICE_ACCOUNT_FILE = "model1-434912-d8821793cd8d.json"
GCP_PROJECT_ID = "model1-434912"  
GCP_ZONE = "us-central1-a"  
GCP_MACHINE_TYPE = "e2-medium"  
GCP_IMAGE_FAMILY = "debian-11"
GCP_IMAGE_PROJECT = "debian-cloud"

# Path to the application files to be synced
APP_PATH = os.path.expanduser("~/auto-scale-project/sample-app/")

def get_resource_usage():
    """Get current system resource usage"""
    cpu_usage = psutil.cpu_percent(interval=1)
    memory_usage = psutil.virtual_memory().percent
    disk_usage = psutil.disk_usage('/').percent
    
    return {
        "cpu": cpu_usage,
        "memory": memory_usage,
        "disk": disk_usage
    }

def get_gcp_credentials():
    """Create and return GCP credentials"""
    try:
        # Load credentials from the service account file
        credentials = service_account.Credentials.from_service_account_file(
            GCP_SERVICE_ACCOUNT_FILE,
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return credentials
    except Exception as e:
        logger.error(f"Error creating GCP credentials: {str(e)}")
        return None

def setup_gcp_instance():
    """Setup a GCP compute instance"""
    logger.info("Setting up GCP instance...")
    
    try:
        # Get credentials
        credentials = get_gcp_credentials()
        if not credentials:
            return None
            
        # Create the clients with the same credentials
        instances_client = compute_v1.InstancesClient(credentials=credentials)
        operation_client = compute_v1.ZoneOperationsClient(credentials=credentials)
        
        # Generate a unique instance name
        instance_name = f"auto-scale-instance-{int(time.time())}"
        
        # Create the instance configuration
        instance_resource = compute_v1.Instance()
        instance_resource.name = instance_name
        instance_resource.machine_type = f"zones/{GCP_ZONE}/machineTypes/{GCP_MACHINE_TYPE}"
        
        # Configure the boot disk
        boot_disk = compute_v1.AttachedDisk()
        boot_disk.boot = True
        boot_disk.auto_delete = True
        boot_disk.initialize_params = compute_v1.AttachedDiskInitializeParams()
        boot_disk.initialize_params.source_image = f"projects/{GCP_IMAGE_PROJECT}/global/images/family/{GCP_IMAGE_FAMILY}"
        instance_resource.disks = [boot_disk]
        
        # Configure network interface
        network_interface = compute_v1.NetworkInterface()
        network_interface.network = "global/networks/default"
        access_config = compute_v1.AccessConfig()
        access_config.name = "External NAT"
        access_config.type_ = "ONE_TO_ONE_NAT"
        network_interface.access_configs = [access_config]
        instance_resource.network_interfaces = [network_interface]
        
        # Configure metadata with startup script
        instance_resource.metadata = compute_v1.Metadata()
        startup_script_item = compute_v1.Items()
        startup_script_item.key = "startup-script"
        startup_script_item.value = "apt-get update && apt-get install -y python3 python3-pip && echo 'Startup complete' > /tmp/startup-complete"
        instance_resource.metadata.items = [startup_script_item]
        
        # Create the instance
        operation = instances_client.insert(
            project=GCP_PROJECT_ID,
            zone=GCP_ZONE,
            instance_resource=instance_resource
        )
        
        # Wait for the operation to complete
        logger.info(f"Creating instance {instance_name}...")
        operation = operation_client.wait(
            project=GCP_PROJECT_ID,
            zone=GCP_ZONE,
            operation=operation.name
        )
        
        if hasattr(operation, 'error') and operation.error:
            logger.error(f"Error creating instance: {operation.error.errors}")
            return None
            
        # Wait a bit longer for the instance to fully initialize and get its IP
        logger.info("Waiting for instance to initialize and get IP address...")
        time.sleep(30)
        
        # Get the created instance
        created_instance = instances_client.get(
            project=GCP_PROJECT_ID, 
            zone=GCP_ZONE, 
            instance=instance_name
        )
        
        # Get the external IP
        external_ip = None
        for network_interface in created_instance.network_interfaces:
            for access_config in network_interface.access_configs:
                if hasattr(access_config, 'nat_ip') and access_config.nat_ip:
                    external_ip = access_config.nat_ip
                    break
            if external_ip:
                break
        
        # If no IP is found, try different approach
        if not external_ip:
            logger.warning("External IP not found in instance object, trying to get it using gcloud CLI...")
            try:
                # Use gcloud CLI to get the external IP
                cmd = f"gcloud compute instances describe {instance_name} --project={GCP_PROJECT_ID} --zone={GCP_ZONE} --format='get(networkInterfaces[0].accessConfigs[0].natIP)'"
                result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
                external_ip = result.stdout.strip()
                if not external_ip:
                    logger.warning("Could not get external IP from gcloud CLI either.")
            except Exception as e:
                logger.error(f"Error getting external IP using gcloud CLI: {str(e)}")
        
        # Create instance info
        instance_info = {
            "id": instance_name,
            "ip": external_ip,
            "status": "running",
            "project": GCP_PROJECT_ID,
            "zone": GCP_ZONE
        }
        
        logger.info(f"Created GCP instance: {instance_info['id']} with IP: {instance_info['ip']}")
        
        # Write instance info to file for future reference
        with open("cloud_instance.json", "w") as f:
            json.dump(instance_info, f, indent=2)
            
        return instance_info
        
    except Exception as e:
        logger.error(f"Error setting up GCP instance: {str(e)}")
        return None

def deploy_application(instance_info):
    """Deploy the application to the GCP instance"""
    logger.info(f"Deploying application to GCP instance {instance_info['id']}...")
    
    # Check if we have an IP address
    if not instance_info['ip']:
        logger.error("Cannot deploy application: No IP address available for the instance")
        return False
    
    try:
        # Authenticate gcloud CLI if needed
        try:
            # Check if already authenticated
            subprocess.run("gcloud auth list", shell=True, check=True, capture_output=True)
        except subprocess.CalledProcessError:
            # Authenticate using service account
            logger.info("Authenticating gcloud CLI with service account...")
            auth_cmd = f"gcloud auth activate-service-account --key-file={GCP_SERVICE_ACCOUNT_FILE}"
            subprocess.run(auth_cmd, shell=True, check=True)
        
        # Wait for SSH to be available
        logger.info(f"Waiting for SSH to be available on {instance_info['ip']}...")
        wait_time = 60  # Wait for 60 seconds
        logger.info(f"Waiting {wait_time} seconds for instance to be ready for SSH...")
        time.sleep(wait_time)
        
        # Create a directory for the app on the remote instance
        ssh_command = f"gcloud compute ssh --quiet --project={instance_info['project']} --zone={instance_info['zone']} {instance_info['id']} --command='mkdir -p ~/app'"
        logger.info(f"Running command: {ssh_command}")
        subprocess.run(ssh_command, shell=True, check=True)
        
        # Copy the application files to the instance
        scp_command = f"gcloud compute scp --quiet --project={instance_info['project']} --zone={instance_info['zone']} --recurse {APP_PATH}/* {instance_info['id']}:~/app/"
        logger.info(f"Copying application files to {instance_info['ip']}...")
        subprocess.run(scp_command, shell=True, check=True)
        
        # Install dependencies and start the application
        setup_command = f"gcloud compute ssh --quiet --project={instance_info['project']} --zone={instance_info['zone']} {instance_info['id']} --command='cd ~/app && pip3 install -r requirements.txt'"
        logger.info(f"Setting up dependencies on {instance_info['ip']}...")
        subprocess.run(setup_command, shell=True, check=True)
        
        # Start the application (assuming there's a main.py file)
        start_command = f"gcloud compute ssh --quiet --project={instance_info['project']} --zone={instance_info['zone']} {instance_info['id']} --command='cd ~/app && nohup python3 main.py > app.log 2>&1 &'"
        logger.info(f"Starting application on {instance_info['ip']}...")
        subprocess.run(start_command, shell=True, check=True)
        
        logger.info("Application successfully deployed!")
        return True
        
    except Exception as e:
        logger.error(f"Error deploying application: {str(e)}")
        return False

def main():
    logger.info("Starting auto-scaling monitor...")
    
    # Set the environment variable for GCP authentication
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GCP_SERVICE_ACCOUNT_FILE
    
    # Authenticate gcloud CLI at startup
    try:
        logger.info("Authenticating gcloud CLI with service account...")
        auth_cmd = f"gcloud auth activate-service-account --key-file={GCP_SERVICE_ACCOUNT_FILE}"
        subprocess.run(auth_cmd, shell=True, check=True)
    except Exception as e:
        logger.error(f"Error authenticating gcloud CLI: {str(e)}")
    
    # Save the initial state
    initial_state = {
        "start_time": time.time(),
        "scaled_to_cloud": False
    }
    
    with open("auto_scale_state.json", "w") as f:
        json.dump(initial_state, f, indent=2)
    
    while True:
        try:
            # Get current resource usage
            usage = get_resource_usage()
            
            logger.info(f"Current usage - CPU: {usage['cpu']:.2f}%, Memory: {usage['memory']:.2f}%, Disk: {usage['disk']:.2f}%")
            
            # Check if any resource exceeds its threshold
            if usage['cpu'] > CPU_THRESHOLD or usage['memory'] > MEM_THRESHOLD or usage['disk'] > DISK_THRESHOLD:
                logger.warning("Resource threshold exceeded!")
                
                # Check if we already scaled to the cloud
                try:
                    with open("auto_scale_state.json", "r") as f:
                        state = json.load(f)
                        
                    if not state.get("scaled_to_cloud", False):
                        logger.info("Scaling to GCP...")
                        
                        # Setup GCP instance
                        instance_info = setup_gcp_instance()
                        
                        if instance_info and instance_info['ip']:
                            # Deploy application
                            if deploy_application(instance_info):
                                # Update state
                                state["scaled_to_cloud"] = True
                                state["instance_info"] = instance_info
                                state["scale_time"] = time.time()
                                
                                with open("auto_scale_state.json", "w") as f:
                                    json.dump(state, f, indent=2)
                                    
                                logger.info("Successfully scaled to GCP!")
                            else:
                                logger.error("Failed to deploy application to GCP instance.")
                        else:
                            logger.error("Failed to create GCP instance or instance has no IP.")
                    else:
                        logger.info("Already scaled to GCP. No action needed.")
                except Exception as e:
                    logger.error(f"Error checking/updating state: {str(e)}")
            
            # Wait before next check
            time.sleep(10)
            
        except Exception as e:
            logger.error(f"Error in main loop: {str(e)}")
            time.sleep(10)

if __name__ == "__main__":
    main()
