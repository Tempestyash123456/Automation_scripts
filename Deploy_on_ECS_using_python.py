#!/usr/bin/env python3
"""
Enhanced ECS Fargate Lifecycle Management Utility
Includes detailed inline instructions for every input.
"""

import boto3
import json
import sys
import time
import re
import argparse
from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound

# ================= COLOR SYSTEM =================
try:
    from colorama import init, Fore, Style
    init(autoreset=True)
except ImportError:
    class Dummy:
        def __getattr__(self, name): return ""
    Fore = Style = Dummy()

RED = Fore.RED
GREEN = Fore.GREEN
YELLOW = Fore.YELLOW
CYAN = Fore.CYAN
MAGENTA = Fore.MAGENTA
WHITE = Fore.WHITE
BOLD = Style.BRIGHT
RESET = Style.RESET_ALL

def banner():
    print(f"""{MAGENTA}{BOLD}
╔══════════════════════════════════════════════════════════════╗
║     🚀 Enhanced ECS Fargate Lifecycle Management Utility 🚀  ║
║            with Step‑by‑Step Deployment Guidance             ║
╚══════════════════════════════════════════════════════════════╝
{RESET}""")

def info(msg): print(f"{CYAN}{BOLD}ℹ {msg}{RESET}")
def success(msg): print(f"{GREEN}{BOLD}✔ {msg}{RESET}")
def warn(msg): print(f"{YELLOW}{BOLD}⚠ {msg}{RESET}")
def error(msg): print(f"{RED}{BOLD}✖ {msg}{RESET}")

def colored_input(label, default=None, required=False, instruction=None):
    """
    Display a prompt with an optional instruction line.
    """
    if instruction:
        print(f"{CYAN}📘 {instruction}{RESET}")
    prompt = f"{WHITE}{BOLD}{label}{RESET}"
    if default:
        prompt += f"{YELLOW} [{default}]{RESET}"
    prompt += f"{CYAN} ➜ {RESET}"
    value = input(prompt)
    if not value and default:
        return default
    if required and not value:
        error("This field is required.")
        sys.exit(1)
    return value

def sanitize_name(name, max_length=32):
    """Convert to ALB‑compatible format (alphanumeric, hyphens only)."""
    s = re.sub(r'[^a-zA-Z0-9\-]', '-', name)
    s = re.sub(r'-+', '-', s).strip('-')
    return s[:max_length]

def check_aws_configured():
    """Verify that AWS credentials are available and valid."""
    try:
        session = boto3.Session()
        sts = session.client('sts')
        sts.get_caller_identity()
        return True
    except (NoCredentialsError, ProfileNotFound, ClientError) as e:
        error("AWS credentials are not configured or are invalid.")
        info("Please run 'aws configure' to set up your credentials.")
        return False

def create_session():
    region = colored_input(
        "AWS Region",
        required=True,
        instruction="The AWS region where all resources (VPC, ECS, ALB) will be created.\n"
                    "Choose the region closest to your users for lower latency."
    )
    return boto3.Session(region_name=region)

def select_vpc_subnets(ec2):
    """Let user choose VPC and subnets interactively."""
    vpcs = ec2.describe_vpcs()["Vpcs"]
    if not vpcs:
        error("No VPCs found in this region.")
        sys.exit(1)

    print("\nAvailable VPCs:")
    for i, vpc in enumerate(vpcs):
        name = " (default)" if vpc.get("IsDefault") else ""
        print(f"{CYAN}{i+1}. {vpc['VpcId']}{name}{RESET}")
    vpc_idx = int(colored_input(
        "Select VPC number",
        required=True,
        instruction="Your ECS tasks and load balancer will be placed inside this VPC.\n"
                    "Choose a VPC that has at least two public subnets for internet accessibility."
    )) - 1
    vpc_id = vpcs[vpc_idx]["VpcId"]

    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    if not subnets:
        error(f"No subnets found in VPC {vpc_id}")
        sys.exit(1)

    print("\nAvailable Subnets (select public ones for internet access):")
    for i, sn in enumerate(subnets):
        public = " (public)" if sn.get("MapPublicIpOnLaunch") else ""
        print(f"{CYAN}{i+1}. {sn['SubnetId']}{public} - {sn['AvailabilityZone']}{RESET}")
    choices = colored_input(
        "Subnet numbers (comma separated, e.g. 1,3)",
        required=True,
        instruction="The load balancer and ECS tasks will be deployed across these subnets.\n"
                    "For internet‑facing applications, select public subnets (they have a route to an Internet Gateway)."
    )
    indices = [int(x.strip())-1 for x in choices.split(",")]
    subnet_ids = [subnets[i]["SubnetId"] for i in indices]
    return vpc_id, subnet_ids

def select_image(session):
    choice = colored_input(
        "Image Source (1=DockerHub, 2=ECR)",
        "1",
        instruction="Where is your container image stored?\n"
                    "1 - DockerHub: a public or private repository on Docker Hub.\n"
                    "2 - Amazon ECR: your private AWS container registry."
    )

    if choice == "1":
        return colored_input(
            "DockerHub Image (e.g., nginx:latest, username/repo:tag)",
            required=True,
            instruction="Enter the full image name including tag. This image must be publicly accessible\n"
                        "or you must have previously authenticated Docker with DockerHub."
        )

    ecr = session.client("ecr")
    repos = ecr.describe_repositories()["repositories"]
    if not repos:
        error("No ECR repositories found.")
        sys.exit(1)

    print("\nAvailable ECR Repositories:")
    for i, repo in enumerate(repos):
        print(f"{CYAN}{i+1}. {repo['repositoryName']}{RESET}")
    idx = int(colored_input(
        "Select repository number",
        required=True,
        instruction="Choose the ECR repository that contains your application image."
    )) - 1
    repo_name = repos[idx]["repositoryName"]

    images = ecr.list_images(repositoryName=repo_name)["imageIds"]
    if not images:
        error("No images found in this repository.")
        sys.exit(1)

    print("\nAvailable Image Tags:")
    for i, img in enumerate(images):
        tag = img.get("imageTag", "<untagged>")
        print(f"{CYAN}{i+1}. {tag}{RESET}")
    tag_idx = int(colored_input(
        "Select image number",
        required=True,
        instruction="Pick the specific image version (tag) you want to deploy."
    )) - 1
    tag = images[tag_idx].get("imageTag", "latest")

    account = session.client("sts").get_caller_identity()["Account"]
    region = session.region_name
    return f"{account}.dkr.ecr.{region}.amazonaws.com/{repo_name}:{tag}"

def parse_ports(raw):
    ports = []
    for token in re.split(r"[,\s]+", raw.strip()):
        if ":" in token:
            token = token.split(":")[-1]
        try:
            port = int(token)
            if 1 <= port <= 65535 and port not in ports:
                ports.append(port)
            else:
                warn(f"Invalid or duplicate port: {token}")
        except ValueError:
            warn(f"Skipping non‑numeric port value: {token}")
    return ports

def parse_env_vars(raw):
    """Parse key=value pairs separated by commas."""
    env = []
    if not raw.strip():
        return env
    for pair in raw.split(","):
        if "=" in pair:
            key, val = pair.split("=", 1)
            env.append({"name": key.strip(), "value": val.strip()})
        else:
            warn(f"Skipping invalid environment variable: {pair}")
    return env

def get_or_create_task_execution_role(iam):
    role_name = "ecsTaskExecutionRole"
    try:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        warn("IAM Task Execution Role already exists.")
    except iam.exceptions.NoSuchEntityException:
        info("Creating IAM Task Execution Role...")
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole"
                }]
            })
        )
        iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
        )
        role_arn = role["Role"]["Arn"]
        time.sleep(8)  # IAM propagation
        success("IAM Task Execution Role created.")
    return role_arn

def get_or_create_task_role(iam, role_name):
    """Create a custom task role if it doesn't exist."""
    try:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        warn(f"IAM Task Role '{role_name}' already exists.")
    except iam.exceptions.NoSuchEntityException:
        info(f"Creating IAM Task Role '{role_name}'...")
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole"
                }]
            })
        )
        # No policies attached by default; user can add later.
        role_arn = role["Role"]["Arn"]
        success(f"IAM Task Role '{role_name}' created (no policies attached).")
    return role_arn

def create_cloudwatch_log_group(logs, log_group_name):
    try:
        logs.create_log_group(logGroupName=log_group_name)
        success(f"CloudWatch Log Group '{log_group_name}' created.")
    except logs.exceptions.ResourceAlreadyExistsException:
        warn(f"Log group '{log_group_name}' already exists.")
    except ClientError as e:
        error(f"Failed to create log group: {e}")
        sys.exit(1)

# ================= DEPLOY =================
def deploy(session):
    ecs = session.client("ecs")
    elb = session.client("elbv2")
    ec2 = session.client("ec2")
    iam = session.client("iam")
    logs = session.client("logs")

    # Basic identification
    cluster = colored_input(
        "Cluster Name",
        "free-tier-cluster",
        instruction="ECS Cluster is a logical grouping of tasks or services.\n"
                    "If it doesn't exist, it will be created. Multiple services can share a cluster."
    )
    service = colored_input(
        "Service Name",
        "free-tier-service",
        instruction="ECS Service ensures that a specified number of tasks are running.\n"
                    "It also integrates with a load balancer for traffic distribution."
    )
    task_family = colored_input(
        "Task Family",
        "free-tier-task",
        instruction="Task Definition family name. Each revision of your task will be registered under this family.\n"
                    "Changing the task definition later (e.g., new image) creates a new revision."
    )
    min_tasks = int(colored_input(
        "Minimum Tasks",
        "1",
        instruction="The desired number of running tasks (instances of your container).\n"
                    "The service will maintain at least this many tasks."
    ))
    max_tasks = int(colored_input(
        "Maximum Tasks (for auto-scaling, same as min if not scaling)",
        str(min_tasks),
        instruction="If you want auto‑scaling, set a number greater than the minimum.\n"
                    "The service will scale between min and max based on CPU utilization."
    ))

    ports_input = colored_input(
        "Container Port(s) to expose (e.g. 80,8080)",
        "80",
        instruction="Port(s) on which your container listens. For each port, a target group will be created.\n"
                    "If you specify multiple ports, each will have its own listener on the load balancer.\n"
                    "Note: All ports must be reachable from the load balancer."
    )
    ports = parse_ports(ports_input)

    if len(ports) == 0:
        error("At least one valid container port is required.")
        sys.exit(1)

    # Environment variables
    env_input = colored_input(
        "Environment variables (key=value, comma separated) [optional]",
        "",
        instruction="Set environment variables inside the container, e.g., DB_URL=mydb,LOG_LEVEL=debug.\n"
                    "Leave empty if none."
    )
    env_vars = parse_env_vars(env_input)

    # CloudWatch logging
    enable_logging = colored_input(
        "Enable CloudWatch logging? (y/n)",
        "y",
        instruction="If enabled, container stdout/stderr will be sent to CloudWatch Logs.\n"
                    "This is useful for debugging and monitoring."
    ).lower() == "y"
    log_group = None
    log_prefix = None
    if enable_logging:
        log_group = colored_input(
            "CloudWatch Log Group name",
            f"/ecs/{service}",
            instruction="Name of the CloudWatch Logs group where logs will be stored.\n"
                        "If it doesn't exist, it will be created automatically."
        )
        log_prefix = colored_input(
            "Log stream prefix",
            "ecs",
            instruction="Prefix for log streams. Each task will have its own stream: {prefix}/{container-name}/{task-id}."
        )
        create_cloudwatch_log_group(logs, log_group)

    # Health check
    health_path = colored_input(
        "Health check path",
        "/",
        instruction="Path that the load balancer will use to check if your container is healthy.\n"
                    "The endpoint should return HTTP 200. Example: /health or /"
    )
    health_interval = int(colored_input(
        "Health check interval (seconds)",
        "30",
        instruction="Time between health checks."
    ))
    health_timeout = int(colored_input(
        "Health check timeout (seconds)",
        "5",
        instruction="Maximum time to wait for a health check response."
    ))
    healthy_threshold = int(colored_input(
        "Healthy threshold count",
        "2",
        instruction="Number of consecutive successful checks before marking a target healthy."
    ))
    unhealthy_threshold = int(colored_input(
        "Unhealthy threshold count",
        "2",
        instruction="Number of consecutive failed checks before marking a target unhealthy."
    ))

    # Image selection
    image = select_image(session)

    # IAM roles
    task_exec_role_arn = get_or_create_task_execution_role(iam)
    use_task_role = colored_input(
        "Create a custom IAM Task Role for your container? (y/n)",
        "n",
        instruction="A task role gives your container permissions to call AWS APIs (e.g., S3, DynamoDB).\n"
                    "If you select 'y', a new IAM role will be created (no policies attached by default).\n"
                    "You can attach policies later via the AWS Console/CLI."
    ).lower() == "y"
    task_role_arn = None
    if use_task_role:
        task_role_name = colored_input(
            "Task Role name",
            f"{task_family}-task-role",
            instruction="Choose a name for the IAM role that your container will assume."
        )
        task_role_arn = get_or_create_task_role(iam, task_role_name)

    # VPC selection
    use_default_vpc = colored_input(
        "Use default VPC? (y/n)",
        "y",
        instruction="Default VPC is simple and pre‑configured with internet access.\n"
                    "Choose 'n' if you want to pick a custom VPC and specific subnets (advanced)."
    ).lower() == "y"
    if use_default_vpc:
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            error("No default VPC found in this region.")
            sys.exit(1)
        vpc_id = vpcs[0]["VpcId"]
        subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
        subnet_ids = [s["SubnetId"] for s in subnets]
        info(f"Using default VPC {vpc_id} with all its subnets.")
    else:
        vpc_id, subnet_ids = select_vpc_subnets(ec2)

    # Security Group (service‑specific)
    sg_name = sanitize_name(f"ecs-sg-{service}", 32)
    try:
        sg = ec2.create_security_group(
            GroupName=sg_name,
            Description=f"Security group for ECS service {service}",
            VpcId=vpc_id
        )
        sg_id = sg["GroupId"]
        success(f"Security group '{sg_name}' created.")
        # Allow ingress on all selected ports (from ALB)
        for port in ports:
            ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{
                    "IpProtocol": "tcp",
                    "FromPort": port,
                    "ToPort": port,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}]
                }]
            )
            success(f"Ingress rule added for port {port} (allows traffic from anywhere).")
    except ClientError as e:
        if "InvalidGroup.Duplicate" in str(e):
            sgs = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [sg_name]}])["SecurityGroups"]
            if sgs:
                sg_id = sgs[0]["GroupId"]
                warn(f"Security group '{sg_name}' already exists. Using it.")
                # Optionally ensure ingress rules exist (skip for brevity)
            else:
                error(f"Security group '{sg_name}' duplicate but not found?")
                sys.exit(1)
        else:
            error(f"Failed to create security group: {e}")
            sys.exit(1)

    # Cluster
    try:
        ecs.create_cluster(clusterName=cluster)
        success(f"Cluster '{cluster}' created.")
    except ecs.exceptions.ClusterAlreadyExistsException:
        warn(f"Cluster '{cluster}' already exists.")

    # Task Definition
    info("Registering task definition...")
    container_def = {
        "name": "app-container",
        "image": image,
        "portMappings": [{"containerPort": p} for p in ports],
        "essential": True,
        "environment": env_vars
    }
    if enable_logging:
        container_def["logConfiguration"] = {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": log_group,
                "awslogs-region": session.region_name,
                "awslogs-stream-prefix": log_prefix
            }
        }

    task_def_kwargs = {
        "family": task_family,
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "256",
        "memory": "512",
        "executionRoleArn": task_exec_role_arn,
        "containerDefinitions": [container_def]
    }
    if task_role_arn:
        task_def_kwargs["taskRoleArn"] = task_role_arn

    try:
        response = ecs.register_task_definition(**task_def_kwargs)
        task_def_arn = response["taskDefinition"]["taskDefinitionArn"]
        success("Task definition registered.")
    except ClientError as e:
        error(f"Failed to register task definition: {e}")
        sys.exit(1)

    # Load Balancer (service‑specific)
    lb_name = sanitize_name(f"ecs-alb-{service}", 32)
    try:
        lb = elb.create_load_balancer(
            Name=lb_name,
            Subnets=subnet_ids,
            SecurityGroups=[sg_id],
            Scheme="internet-facing",
            Type="application",
            Tags=[{"Key": "Service", "Value": service}]
        )
        success(f"Load Balancer '{lb_name}' created.")
        waiter = elb.get_waiter('load_balancer_exists')
        waiter.wait(Names=[lb_name])
        lb_arn = lb["LoadBalancers"][0]["LoadBalancerArn"]
    except ClientError as e:
        if "DuplicateLoadBalancerName" in str(e):
            lbs = elb.describe_load_balancers(Names=[lb_name])
            lb_arn = lbs["LoadBalancers"][0]["LoadBalancerArn"]
            warn(f"Load Balancer '{lb_name}' already exists.")
        else:
            error(f"Failed to create load balancer: {e}")
            sys.exit(1)

    dns_name = elb.describe_load_balancers(LoadBalancerArns=[lb_arn])["LoadBalancers"][0]["DNSName"]

    # HTTPS support
    use_https = colored_input(
        "Enable HTTPS? (requires ACM certificate) (y/n)",
        "n",
        instruction="If you have an SSL/TLS certificate from AWS Certificate Manager (ACM),\n"
                    "you can enable HTTPS on port 443. You will also have the option to redirect HTTP to HTTPS."
    ).lower() == "y"
    cert_arn = None
    redirect_http = False
    if use_https:
        cert_arn = colored_input(
            "ACM Certificate ARN",
            required=True,
            instruction="Paste the full ARN of your ACM certificate (e.g., arn:aws:acm:region:account:certificate/...).\n"
                        "The certificate must be in the same region and cover your domain."
        )
        redirect_http = colored_input(
            "Redirect HTTP (port 80) to HTTPS? (y/n)",
            "y",
            instruction="If you enable this, any request on port 80 will be redirected to HTTPS (port 443).\n"
                        "Otherwise, you will have separate listeners for HTTP and HTTPS."
        ).lower() == "y"

    # Target Groups and Listeners for each port
    target_group_arns = []
    for port in ports:
        tg_name = sanitize_name(f"ecs-tg-{service}-{port}", 32)
        try:
            tg = elb.create_target_group(
                Name=tg_name,
                Protocol="HTTP",
                Port=port,
                VpcId=vpc_id,
                TargetType="ip",
                HealthCheckPath=health_path,
                HealthCheckIntervalSeconds=health_interval,
                HealthCheckTimeoutSeconds=health_timeout,
                HealthyThresholdCount=healthy_threshold,
                UnhealthyThresholdCount=unhealthy_threshold
            )
            tg_arn = tg["TargetGroups"][0]["TargetGroupArn"]
            success(f"Target group '{tg_name}' created for port {port}.")
        except ClientError as e:
            if "DuplicateTargetGroupName" in str(e):
                tgs = elb.describe_target_groups(Names=[tg_name])
                tg_arn = tgs["TargetGroups"][0]["TargetGroupArn"]
                warn(f"Target group '{tg_name}' already exists.")
            else:
                error(f"Failed to create target group: {e}")
                sys.exit(1)

        target_group_arns.append(tg_arn)

        # Create listener for this port
        try:
            # Check if listener already exists on this port
            listeners = elb.describe_listeners(LoadBalancerArn=lb_arn)["Listeners"]
            existing = any(lis["Port"] == port for lis in listeners)
            if not existing:
                if use_https and port == 443:
                    # HTTPS listener
                    elb.create_listener(
                        LoadBalancerArn=lb_arn,
                        Protocol="HTTPS",
                        Port=443,
                        Certificates=[{"CertificateArn": cert_arn}],
                        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}]
                    )
                    success(f"HTTPS listener created on port 443.")
                elif use_https and port == 80 and redirect_http:
                    # HTTP to HTTPS redirect
                    elb.create_listener(
                        LoadBalancerArn=lb_arn,
                        Protocol="HTTP",
                        Port=80,
                        DefaultActions=[{
                            "Type": "redirect",
                            "RedirectConfig": {
                                "Protocol": "HTTPS",
                                "Port": "443",
                                "StatusCode": "HTTP_301"
                            }
                        }]
                    )
                    success("HTTP listener created with redirect to HTTPS.")
                else:
                    # Plain HTTP listener
                    elb.create_listener(
                        LoadBalancerArn=lb_arn,
                        Protocol="HTTP",
                        Port=port,
                        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}]
                    )
                    success(f"HTTP listener created on port {port}.")
            else:
                warn(f"Listener on port {port} already exists.")
        except ClientError as e:
            error(f"Failed to create listener for port {port}: {e}")
            sys.exit(1)

    # ECS Service
    info("Creating ECS service...")
    load_balancers = [{
        "targetGroupArn": target_group_arns[i],
        "containerName": "app-container",
        "containerPort": ports[i]
    } for i in range(len(ports))]

    try:
        ecs.create_service(
            cluster=cluster,
            serviceName=service,
            taskDefinition=task_family,  # Use family (latest active revision)
            desiredCount=min_tasks,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": subnet_ids,
                    "securityGroups": [sg_id],
                    "assignPublicIp": "ENABLED"
                }
            },
            loadBalancers=load_balancers,
            deploymentConfiguration={
                "maximumPercent": 200,
                "minimumHealthyPercent": 100
            }
        )
        success(f"Service '{service}' created.")
    except ClientError as e:
        if "ServiceAlreadyExists" in str(e):
            warn(f"Service '{service}' already exists.")
            update = colored_input(
                "Update existing service with new task definition? (y/n)",
                "y",
                instruction="If you select 'y', the service will be updated to use the latest task definition revision.\n"
                            "A new deployment will be triggered."
            )
            if update.lower() == "y":
                ecs.update_service(
                    cluster=cluster,
                    service=service,
                    taskDefinition=task_family,
                    forceNewDeployment=True
                )
                success("Service updated.")
        else:
            error(f"Failed to create service: {e}")
            sys.exit(1)

    # Auto Scaling (if max_tasks > min_tasks)
    if max_tasks > min_tasks:
        info("Configuring Application Auto Scaling...")
        as_client = session.client("application-autoscaling")
        # Register scalable target
        try:
            as_client.register_scalable_target(
                ServiceNamespace="ecs",
                ResourceId=f"service/{cluster}/{service}",
                ScalableDimension="ecs:service:DesiredCount",
                MinCapacity=min_tasks,
                MaxCapacity=max_tasks
            )
            success("Auto scaling target registered.")
            # Simple scaling policy: target CPU 70%
            as_client.put_scaling_policy(
                PolicyName="cpu-target-policy",
                ServiceNamespace="ecs",
                ResourceId=f"service/{cluster}/{service}",
                ScalableDimension="ecs:service:DesiredCount",
                PolicyType="TargetTrackingScaling",
                TargetTrackingScalingPolicyConfiguration={
                    "TargetValue": 70.0,
                    "PredefinedMetricSpecification": {
                        "PredefinedMetricType": "ECSServiceAverageCPUUtilization"
                    },
                    "ScaleOutCooldown": 60,
                    "ScaleInCooldown": 60
                }
            )
            success("Auto scaling policy added (target CPU 70%).")
        except ClientError as e:
            error(f"Failed to configure auto scaling: {e}")

    # Output URL
    protocol = "https" if use_https and 443 in ports else "http"
    port_display = "" if (protocol == "http" and 80 in ports) or (protocol == "https" and 443 in ports) else f":{ports[0]}"
    print(f"\n{GREEN}{BOLD}🌍 Application URL: {protocol}://{dns_name}{port_display}{RESET}\n")
    info("It may take a minute or two for the service to become healthy.")


# ================= DESTROY =================
def destroy(session):
    ecs = session.client("ecs")
    elb = session.client("elbv2")
    ec2 = session.client("ec2")
    logs = session.client("logs")
    as_client = session.client("application-autoscaling")

    cluster = colored_input(
        "Cluster Name",
        "free-tier-cluster",
        instruction="Name of the ECS cluster that contained your service."
    )
    service = colored_input(
        "Service Name",
        "free-tier-service",
        instruction="Name of the ECS service you want to destroy."
    )
    task_family = colored_input(
        "Task Family",
        "free-tier-task",
        instruction="Task family used by the service (to deregister task definitions)."
    )
    ports_input = colored_input(
        "Container Port(s) that were exposed (comma separated)",
        required=True,
        instruction="Ports you used when creating the service. This helps identify the target groups to delete."
    )
    ports = parse_ports(ports_input)

    info("Destroying infrastructure...")

    # 1. Delete auto scaling (if any)
    try:
        as_client.deregister_scalable_target(
            ServiceNamespace="ecs",
            ResourceId=f"service/{cluster}/{service}",
            ScalableDimension="ecs:service:DesiredCount"
        )
        success("Auto scaling target deregistered.")
    except ClientError:
        pass

    # 2. Scale down and delete service
    try:
        ecs.update_service(cluster=cluster, service=service, desiredCount=0)
        time.sleep(5)
    except ClientError:
        pass
    try:
        ecs.delete_service(cluster=cluster, service=service, force=True)
        success("Service deleted.")
    except ClientError as e:
        if "ServiceNotFoundException" in str(e):
            warn("Service not found.")
        else:
            error(f"Failed to delete service: {e}")

    # 3. Deregister task definitions
    try:
        arns = ecs.list_task_definitions(familyPrefix=task_family)["taskDefinitionArns"]
        for arn in arns:
            ecs.deregister_task_definition(taskDefinition=arn)
        success("Task definitions deregistered.")
    except ClientError:
        warn("No task definitions found or error during deregistration.")

    # 4. Delete cluster if empty
    try:
        ecs.delete_cluster(cluster=cluster)
        success(f"Cluster '{cluster}' deleted.")
    except ClientError as e:
        if "ClusterNotFoundException" in str(e):
            warn("Cluster not found.")
        else:
            error(f"Failed to delete cluster: {e}")

    # 5. Delete load balancer (service‑specific)
    lb_name = sanitize_name(f"ecs-alb-{service}", 32)
    try:
        lbs = elb.describe_load_balancers(Names=[lb_name])
        lb_arn = lbs["LoadBalancers"][0]["LoadBalancerArn"]

        # Delete listeners first
        listeners = elb.describe_listeners(LoadBalancerArn=lb_arn)["Listeners"]
        for lis in listeners:
            elb.delete_listener(ListenerArn=lis["ListenerArn"])
        # Delete ALB
        elb.delete_load_balancer(LoadBalancerArn=lb_arn)
        waiter = elb.get_waiter('load_balancers_deleted')
        waiter.wait(LoadBalancerArns=[lb_arn])
        success(f"Load balancer '{lb_name}' deleted.")
    except ClientError as e:
        if "LoadBalancerNotFoundException" in str(e):
            warn("Load balancer not found.")
        else:
            error(f"Failed to delete load balancer: {e}")

    # 6. Delete target groups
    for port in ports:
        tg_name = sanitize_name(f"ecs-tg-{service}-{port}", 32)
        try:
            tgs = elb.describe_target_groups(Names=[tg_name])
            tg_arn = tgs["TargetGroups"][0]["TargetGroupArn"]
            elb.delete_target_group(TargetGroupArn=tg_arn)
            success(f"Target group '{tg_name}' deleted.")
        except ClientError as e:
            if "TargetGroupNotFoundException" in str(e):
                warn(f"Target group '{tg_name}' not found.")
            else:
                error(f"Failed to delete target group '{tg_name}': {e}")

    # 7. Delete security group
    sg_name = sanitize_name(f"ecs-sg-{service}", 32)
    try:
        sgs = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [sg_name]}])["SecurityGroups"]
        if sgs:
            sg_id = sgs[0]["GroupId"]
            ec2.delete_security_group(GroupId=sg_id)
            success(f"Security group '{sg_name}' deleted.")
        else:
            warn("Security group not found.")
    except ClientError as e:
        error(f"Failed to delete security group: {e}")

    # 8. Delete CloudWatch log group (if it exists)
    log_group = f"/ecs/{service}"
    try:
        logs.delete_log_group(logGroupName=log_group)
        success(f"Log group '{log_group}' deleted.")
    except ClientError as e:
        if "ResourceNotFoundException" in str(e):
            warn("Log group not found.")
        else:
            error(f"Failed to delete log group: {e}")

    print(f"\n{GREEN}{BOLD}🔥 Infrastructure Destroyed Successfully.{RESET}\n")


# ================= MAIN =================
def main():
    parser = argparse.ArgumentParser(description="Enhanced ECS Fargate deployment with multi‑port, HTTPS, custom VPC, logging.")
    parser.add_argument("--destroy", action="store_true", help="Destroy an existing deployment")
    args = parser.parse_args()

    banner()

    if not check_aws_configured():
        sys.exit(1)

    session = create_session()

    if args.destroy:
        destroy(session)
    else:
        deploy(session)


if __name__ == "__main__":
    main()
