#!/usr/bin/env python3

import boto3
import json
import sys
import time
import getpass
import re
import argparse
from botocore.exceptions import ClientError

# ================= COLOR SYSTEM =================
try:
    from colorama import init, Fore, Style
    init(autoreset=True)
except:
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
╔══════════════════════════════════════════════════════╗
║     🚀 ECS Fargate Lifecycle Management Utility 🚀   ║
╚══════════════════════════════════════════════════════╝
{RESET}""")

def info(msg): print(f"{CYAN}{BOLD}ℹ {msg}{RESET}")
def success(msg): print(f"{GREEN}{BOLD}✔ {msg}{RESET}")
def warn(msg): print(f"{YELLOW}{BOLD}⚠ {msg}{RESET}")
def error(msg): print(f"{RED}{BOLD}✖ {msg}{RESET}")

def colored_input(label, default=None, required=False):
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

# ================= SESSION =================
def create_session():
    region = colored_input("AWS Region (e.g. us-east-1)", required=True)
    return boto3.Session(region_name=region)

# ================= IMAGE SELECTION =================
def select_image(session):
    choice = colored_input("Image Source (1=DockerHub, 2=ECR)", "1")

    if choice == "1":
        return colored_input("DockerHub Image (nginx:latest)", required=True)

    ecr = session.client("ecr")
    repos = ecr.describe_repositories()["repositories"]
    if not repos:
        error("No ECR repositories found.")
        sys.exit(1)

    print("\nAvailable ECR Repositories:")
    for i, repo in enumerate(repos):
        print(f"{CYAN}{i+1}. {repo['repositoryName']}{RESET}")

    idx = int(colored_input("Select repository number", required=True)) - 1
    repo_name = repos[idx]["repositoryName"]

    images = ecr.list_images(repositoryName=repo_name)["imageIds"]

    print("\nAvailable Image Tags:")
    for i, img in enumerate(images):
        tag = img.get("imageTag", "<untagged>")
        print(f"{CYAN}{i+1}. {tag}{RESET}")

    tag_idx = int(colored_input("Select image number", required=True)) - 1
    tag = images[tag_idx].get("imageTag", "latest")

    account = session.client("sts").get_caller_identity()["Account"]
    region = session.region_name

    return f"{account}.dkr.ecr.{region}.amazonaws.com/{repo_name}:{tag}"

# ================= PORT PARSER =================
def parse_ports(raw):
    ports = []
    for token in re.split(r"[,\s]+", raw.strip()):
        if ":" in token:
            token = token.split(":")[-1]
        port = int(token)
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return ports

# ================= DEPLOY =================
def deploy(session):
    ecs = session.client("ecs")
    elb = session.client("elbv2")
    ec2 = session.client("ec2")
    iam = session.client("iam")

    cluster = colored_input("Cluster Name", "free-tier-cluster")
    service = colored_input("Service Name", "free-tier-service")
    task_family = colored_input("Task Family", "free-tier-task")
    min_tasks = int(colored_input("Minimum Tasks", "1"))
    ports = parse_ports(colored_input("Container Port on which application is running (e.g. 80,8080)", "80"))

    image = select_image(session)

    # IAM ROLE
    role_name = "ecsTaskExecutionRole"
    try:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        warn("IAM Role already exists.")
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps({
                "Version":"2012-10-17",
                "Statement":[{
                    "Effect":"Allow",
                    "Principal":{"Service":"ecs-tasks.amazonaws.com"},
                    "Action":"sts:AssumeRole"
                }]
            })
        )
        iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
        )
        role_arn = role["Role"]["Arn"]
        time.sleep(8)
        success("IAM Role created.")

    # CLUSTER
    try:
        ecs.create_cluster(clusterName=cluster)
        success("Cluster created.")
    except ecs.exceptions.ClusterAlreadyExistsException:
        warn("Cluster already exists.")

    # TASK DEFINITION
    ecs.register_task_definition(
        family=task_family,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu="256",
        memory="512",
        executionRoleArn=role_arn,
        containerDefinitions=[{
            "name":"app-container",
            "image":image,
            "portMappings":[{"containerPort":p} for p in ports],
            "essential":True
        }]
    )

    # NETWORK
    vpc = ec2.describe_vpcs(Filters=[{"Name":"isDefault","Values":["true"]}])["Vpcs"][0]
    vpc_id = vpc["VpcId"]
    subnets = ec2.describe_subnets(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])["Subnets"]
    subnet_ids = [s["SubnetId"] for s in subnets]

    sg_name = "ecs-alb-sg"
    sgs = ec2.describe_security_groups(Filters=[{"Name":"group-name","Values":[sg_name]}])["SecurityGroups"]

    if sgs:
        sg_id = sgs[0]["GroupId"]
        warn("Security Group already exists.")
    else:
        sg_id = ec2.create_security_group(
            GroupName=sg_name,
            Description="ECS ALB Security Group",
            VpcId=vpc_id
        )["GroupId"]
        success("Security Group created.")

    # Allow HTTP 80 only (Recommended Architecture)
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{
                "IpProtocol":"tcp",
                "FromPort":80,
                "ToPort":80,
                "IpRanges":[{"CidrIp":"0.0.0.0/0"}]
            }]
        )
    except ClientError:
        pass

    # LOAD BALANCER
    lb_name = "ecs-alb"
    try:
        lb = elb.create_load_balancer(
            Name=lb_name,
            Subnets=subnet_ids,
            SecurityGroups=[sg_id],
            Scheme="internet-facing",
            Type="application"
        )
        success("Load Balancer created.")
    except ClientError:
        lb = elb.describe_load_balancers(Names=[lb_name])
        warn("Load Balancer already exists.")

    lb_arn = lb["LoadBalancers"][0]["LoadBalancerArn"]
    dns = lb["LoadBalancers"][0]["DNSName"]

    # TARGET GROUPS
    tg_arns = []
    for p in ports:
        tg_name = f"ecs-target-group-{p}"
        try:
            tg = elb.create_target_group(
                Name=tg_name,
                Protocol="HTTP",
                Port=p,
                VpcId=vpc_id,
                TargetType="ip"
            )
            success(f"Target Group {p} created.")
        except ClientError:
            tg = elb.describe_target_groups(Names=[tg_name])
            warn(f"Target Group {p} already exists.")

        tg_arns.append(tg["TargetGroups"][0]["TargetGroupArn"])

    # LISTENER ON PORT 80
    try:
        elb.create_listener(
            LoadBalancerArn=lb_arn,
            Protocol="HTTP",
            Port=80,
            DefaultActions=[{
                "Type":"forward",
                "TargetGroupArn":tg_arns[0]
            }]
        )
        success("Listener created on port 80.")
    except ClientError:
        warn("Listener on port 80 already exists.")

    # SERVICE
    try:
        ecs.create_service(
            cluster=cluster,
            serviceName=service,
            taskDefinition=task_family,
            desiredCount=min_tasks,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration":{
                    "subnets":subnet_ids,
                    "securityGroups":[sg_id],
                    "assignPublicIp":"ENABLED"
                }
            },
            loadBalancers=[{
                "targetGroupArn":tg_arns[0],
                "containerName":"app-container",
                "containerPort":ports[0]
            }]
        )
        success("Service created.")
    except ecs.exceptions.ServiceAlreadyExistsException:
        warn("Service already exists.")

    print(f"\n{GREEN}{BOLD}🌍 Application URL: http://{dns}{RESET}\n")

# ================= DESTROY =================
def destroy(session):
    ecs = session.client("ecs")
    elb = session.client("elbv2")
    ec2 = session.client("ec2")

    cluster = colored_input("Cluster Name", "free-tier-cluster")
    service = colored_input("Service Name", "free-tier-service")
    task_family = colored_input("Task Family", "free-tier-task")

    info("Destroying infrastructure...")

    try:
        ecs.update_service(cluster=cluster, service=service, desiredCount=0)
        time.sleep(5)
    except:
        pass

    try:
        ecs.delete_service(cluster=cluster, service=service, force=True)
        success("Service deleted.")
    except:
        pass

    try:
        lbs = elb.describe_load_balancers(Names=["ecs-alb"])
        lb_arn = lbs["LoadBalancers"][0]["LoadBalancerArn"]
        listeners = elb.describe_listeners(LoadBalancerArn=lb_arn)["Listeners"]
        for l in listeners:
            elb.delete_listener(ListenerArn=l["ListenerArn"])
        elb.delete_load_balancer(LoadBalancerArn=lb_arn)
        success("Load Balancer deleted.")
    except:
        pass

    try:
        tgs = elb.describe_target_groups()["TargetGroups"]
        for tg in tgs:
            if tg["TargetGroupName"].startswith("ecs-target-group"):
                elb.delete_target_group(TargetGroupArn=tg["TargetGroupArn"])
        success("Target Groups deleted.")
    except:
        pass

    try:
        arns = ecs.list_task_definitions(familyPrefix=task_family)["taskDefinitionArns"]
        for arn in arns:
            ecs.deregister_task_definition(taskDefinition=arn)
    except:
        pass

    try:
        ecs.delete_cluster(cluster=cluster)
        success("Cluster deleted.")
    except:
        pass

    try:
        sgs = ec2.describe_security_groups(Filters=[{"Name":"group-name","Values":["ecs-alb-sg"]}])["SecurityGroups"]
        if sgs:
            ec2.delete_security_group(GroupId=sgs[0]["GroupId"])
            success("Security Group deleted.")
    except:
        pass

    print(f"\n{GREEN}{BOLD}🔥 Infrastructure Destroyed Successfully.{RESET}\n")

# ================= MAIN =================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destroy", action="store_true")
    args = parser.parse_args()

    banner()
    session = create_session()

    if args.destroy:
        destroy(session)
    else:
        deploy(session)

if __name__ == "__main__":
    main()