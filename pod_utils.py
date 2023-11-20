import yaml
import base64
import random
import string
from kubernetes import client, config


def generate_password(length: int):
    characters = string.ascii_letters + string.digits

    generated_password = ''.join(random.choice(characters) for _ in range(length))
    return generated_password


def create_deployment(uid: str):
    with open("pod/deployment.yaml") as file:
        yaml_content = yaml.safe_load(file)

    yaml_content["metadata"]["name"] = f"deployment-{uid}"
    yaml_content["metadata"]["labels"]["app"] = uid
    yaml_content["spec"]["selector"]["matchLabels"]["app"] = uid
    yaml_content["spec"]["template"]["metadata"]["labels"]["app"] = uid
    yaml_content["spec"]["template"]["spec"]["containers"][0]["name"] = uid
    yaml_content["spec"]["template"]["spec"]["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"]["name"] = \
        f"secret-{uid}"
    yaml_content["spec"]["template"]["spec"]["containers"][0]["env"][1]["valueFrom"]["secretKeyRef"]["name"] = \
        f"secret-{uid}"
    yaml_content["spec"]["template"]["spec"]["containers"][0]["env"][2]["valueFrom"]["secretKeyRef"]["name"] = \
        f"secret-{uid}"
    yaml_content["spec"]["template"]["spec"]["containers"][0]["env"][3]["valueFrom"]["secretKeyRef"]["name"] = \
        f"secret-{uid}"

    return yaml_content


def create_service(uid: str, namespace: str):
    config.load_incluster_config()
    port_range = [i for i in range(49154, 49175)]
    used_ports = []
    unused_port = None

    core_v1_api = client.CoreV1Api()

    service_list = core_v1_api.list_namespaced_service(namespace=namespace)

    for service in service_list.items:
        for port in service.spec.ports:
            used_ports.append(int(port.port))

    # Checks first unused port
    for des_port in port_range:
        if des_port not in used_ports:
            unused_port = des_port
            break

    if unused_port is not None:
        with open("pod/service.yaml") as file:
            yaml_content = yaml.safe_load(file)

        yaml_content["metadata"]["name"] = f"service-{uid}"
        yaml_content["spec"]["ports"][0]["port"] = unused_port
        yaml_content["spec"]["selector"]["app"] = uid

        return yaml_content, unused_port
    else:
        return None


def create_secret(uid: str):
    generated_password = generate_password(12)

    with open("pod/secret.yaml") as file:
        yaml_content = yaml.safe_load(file)

        yaml_content["metadata"]["name"] = f"secret-{uid}"
        encoded_bytes = base64.b64encode(generated_password.encode('utf-8'))
        encoded_jupyter_token = encoded_bytes.decode('utf-8')
        yaml_content["data"]["jupyter_token"] = encoded_jupyter_token

        encoded_notebook_id_bytes = base64.b64encode(uid.encode('utf-8'))
        encoded_notebook_id = encoded_notebook_id_bytes.decode('utf-8')
        yaml_content["data"]["notebook_id"] = encoded_notebook_id

        encoded_service_name_bytes = base64.b64encode("api-deleter-service".encode('utf-8'))
        encoded_service_name = encoded_service_name_bytes.decode('utf-8')
        yaml_content["data"]["service_name"] = encoded_service_name

        port = 49153
        encoded_service_port_bytes = base64.b64encode(port.to_bytes(5, byteorder="big").decode("utf-8").encode("utf-8"))
        encoded_service_port = encoded_service_port_bytes.decode('utf-8')
        yaml_content["data"]["service_port"] = encoded_service_port

    return yaml_content, generated_password


def create_ingress(uid: str, port: int):
    with open("pod/ingress.yaml") as file:
        yaml_content = yaml.safe_load(file)

        yaml_content["metadata"]["name"] = f"ingress-{uid}"
        yaml_content["spec"]["rules"][0]["http"]["paths"][0]["path"] = f"/{uid}"
        yaml_content["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"] = f"service-{uid}"
        yaml_content["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"]["number"] = port

    return yaml_content
