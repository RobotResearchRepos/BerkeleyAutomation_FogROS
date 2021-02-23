#!/usr/bin/python

import rospy
import re
import shutil
import rospkg
import boto3
from botocore.exceptions import ClientError
import os
import paramiko
from scp import SCPClient
from io import StringIO
from requests import get
import time

def make_zip_file(dir_name, target_path):
    pwd, package_name = os.path.split(dir_name)
    return shutil.make_archive(base_dir = package_name, root_dir = pwd, format = "zip", base_name = target_path)

if __name__ == '__main__':
    rospy.init_node('roscloud')
   
    #
    # read in parameters from the launch script
    #
    TO_CLOUD_LAUNCHFILE_NAME = rospy.get_param('~to_cloud_launchfile_name', "to_cloud.launch")

    MY_IP_ADDR = get('https://api.ipify.org').text
    print("my ip address is ", MY_IP_ADDR)
    ZIP_FILE_TMP_PATH = rospy.get_param('~temporary_dir', "/tmp")
    image_id = rospy.get_param('~ec2_instance_image', 'ami-05829bd3e68bcd415')
    ec2_instance_type = rospy.get_param('~ec2_instance_type', 't2.micro')
    # name of existing key pair
    # TODO: get a new one if this paramter is not there
    ec2_key_name = rospy.get_param('~ec2_key_name')

    import random
    rand_int = str(random.randint(10, 1000))
    ec2_key_name = "foo" + rand_int
    ec2 = boto3.client('ec2', "us-west-1")
    ec2_keypair = ec2.create_key_pair(KeyName=ec2_key_name) 
    ec2_priv_key = ec2_keypair['KeyMaterial']
    with open("/home/ubuntu/" + ec2_key_name + ".pem", "w") as f:
        f.write(ec2_priv_key)
    print(ec2_priv_key)

    ec2_security_group_ids = rospy.get_param('~ec2_security_group_ids', [])
    if not ec2_security_group_ids:
        response = ec2.describe_vpcs()
        vpc_id = response.get('Vpcs', [{}])[0].get('VpcId', '')

        try:
            response = ec2.create_security_group(GroupName='SECURITY_GROUP_NAME'+rand_int,
                                         Description='DESCRIPTION',
                                         VpcId=vpc_id)
            security_group_id = response['GroupId']
            print('Security Group Created %s in vpc %s.' % (security_group_id, vpc_id))

            data = ec2.authorize_security_group_ingress(
                GroupId=security_group_id,
                IpPermissions=[
                    {'IpProtocol': '-1',
                     'FromPort': 0,
                     'ToPort': 65535,
                     'IpRanges': [{'CidrIp': '0.0.0.0/0'}]
                    }
                ])
            print('Ingress Successfully Set %s' % data)
            ec2_security_group_ids = [security_group_id]
        except ClientError as e:
            print(e)
    print("security group id is " + str(ec2_security_group_ids))
            
    #
    # start EC2 instance
    # note that we can start muliple instances at the same time
    #
    ec2_instance_type ="t2.large"
    ec2_resource = boto3.resource('ec2', "us-west-1")
    instances = ec2_resource.create_instances(
        ImageId=image_id,
        MinCount=1,
        MaxCount=1,
        InstanceType=ec2_instance_type,
        KeyName= ec2_key_name,
        SecurityGroupIds= ec2_security_group_ids
    )
    print("Have created the instance: ", instances)
    instance = instances[0]
    # use the boto3 waiter
    print("wait for launching to finish")
    instance.wait_until_running()
    print("launch finished")
    # reload instance object
    instance.reload()
    #instance_dict = ec2.describe_instances().get('Reservations')[0]
    #print(instance_dict)
    

    # 
    # read in the launchfile 
    # we also modify the launchfile IP address to this machine's public IP address
    # 
    launch_file = rospy.get_param('~launch_file')
    
    with open(launch_file) as f:
        launch_text = f.read()
        launch_file_dir , launch_file_name = os.path.split(launch_file)
    # TODO: change the modified launch file address to a temporary folder
    with open(launch_file_dir + "/" + TO_CLOUD_LAUNCHFILE_NAME , "w") as f:
        f.write(launch_text.replace("ROSBRIDGE_IP_ADDR_REPLACE", MY_IP_ADDR))
        
    # find all the ROS packages in the launchscript
    # package need to follow ros naming convention
    # i.e. flat namespace with lower case letters and underscore separators
    # then zip all the packages
    # currently we assume all the packages can be catkin_make 
    rospack = rospkg.RosPack()
    packages = set(re.findall(r"pkg=\"[a-z_]*\"" ,launch_text))
    packages.add("pkg=\"roscloud\"")
    print(packages)
    zip_paths = []
    for package in packages:
        package = package.split("\"")[1]
        pkg_path = rospack.get_path(package)
        zip_path = make_zip_file(pkg_path, "/tmp/" + package)
        zip_paths.append(zip_path)


    # get public ip address of the EC2 server
    #instance_id = "i-0830a57e084eb8799"
    #instance = ec2_resource.Instance(instance_id)
    
    public_ip = instance.public_ip_address
    while not public_ip:
        instance.reload()
        public_ip = instance.public_ip_address
        print(public_ip)

    #keyfile = StringIO()
    #keyfile.write(ec2_priv_key)
    #keyfile.seek(0)
    # start a SSH/SCP session to the EC2 server 
    #private_key = paramiko.RSAKey.from_private_key(keyfile) ./priv_key.pem
    time.sleep(20)
    private_key = paramiko.RSAKey.from_private_key_file("/home/ubuntu/" + ec2_key_name + ".pem")
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh_client.connect(hostname = public_ip, username = "ubuntu", pkey = private_key, look_for_keys=False )

    with SCPClient(ssh_client.get_transport()) as scp:
        # transfer all the zip files to the EC2 server's workspace 
        for zip_file in zip_paths:
            scp.put(zip_file, '~/catkin_ws/src')

        # use SCP to upload the launch script
        scp.put(launch_file_dir + "/" + TO_CLOUD_LAUNCHFILE_NAME, "~/catkin_ws/src/roscloud/launch/" + TO_CLOUD_LAUNCHFILE_NAME)

        scp.put(launch_file_dir + "/setup.bash", "~/setup.bash")

            
        # use SSH to unzip them to the catkin workspace
        stdin, stdout, stderr = ssh_client.exec_command("cd ~/catkin_ws/src && for i in *.zip; do unzip -o \"$i\" -d . ; done " , get_pty=True)

        CRED = '\033[91m'
        CEND = '\033[0m'
        for line in iter(stdout.readline, ""):
            print(CRED + line + CEND, end="")

        
        # execute setup script
        stdin, stdout, stderr = ssh_client.exec_command("chmod +x ~/setup.bash && ~/setup.bash" , get_pty=True)

        for line in iter(stdout.readline, ""):
            print(CRED + line + CEND, end="")


        # catkin_make all the uploaded packages
        # roslaunch the script on EC2  
        stdin, stdout, stderr = ssh_client.exec_command('cd ~/catkin_ws/ && source ./devel/setup.bash && catkin_make && roslaunch roscloud ' + TO_CLOUD_LAUNCHFILE_NAME , get_pty=True)

        for line in iter(stdout.readline, ""):
            print("EC2: " + CRED + line + CEND, end="")        
