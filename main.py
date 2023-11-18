import os
import uvicorn
from uuid import uuid4
from fastapi import FastAPI
import datetime
from dotenv import load_dotenv
from pod_utils import create_service, create_deployment, create_secret, create_ingress
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from starlette.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import create_engine, Column, String, DateTime, Boolean

app = FastAPI()
Base = declarative_base()
load_dotenv()
namespace = os.getenv("NAMESPACE")
engine = create_engine(f'postgresql+psycopg2://'
                       f'{os.getenv("POSTGRES_USER")}:{os.getenv("POSTGRES_PASSWORD")}@{os.getenv("POSTGRES_HOST")}'
                       f':{os.getenv("POSTGRES_PORT")}/{os.getenv("POSTGRES_DB")}')
Session = sessionmaker(bind=engine)


class MyTable(Base):
    __tablename__ = "notebooks"
    notebook_id = Column(String, primary_key=True)
    user_id = Column(String)
    done = Column(Boolean)
    created_at = Column(DateTime)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)


@app.get("/")
async def connection_test():
    return JSONResponse("Server Works!", status_code=200)


@app.put("/create_notebook_instance")
async def create_notebook_instance(user_id: str):
    config.load_incluster_config()
    apps_v1_api = client.AppsV1Api()
    core_v1_api = client.CoreV1Api()
    networking_v1_api = client.NetworkingV1Api()

    uid = str(uuid4())
    deployment_body = create_deployment(uid)
    service_body, service_port = create_service(uid, namespace=namespace)
    secret_body, password = create_secret(uid)
    ingress_body = create_ingress(uid, service_port)
    if service_body is None:
        return JSONResponse(content="Could not deploy a new instance!", status_code=500)
    try:
        core_v1_api.create_namespaced_secret(namespace=namespace, body=secret_body)
        apps_v1_api.create_namespaced_deployment(namespace=namespace, body=deployment_body)
        core_v1_api.create_namespaced_service(namespace=namespace, body=service_body)
        networking_v1_api.create_namespaced_ingress(namespace=namespace, body=ingress_body)
    except ApiException as e:
        try:
            networking_v1_api.delete_namespaced_ingress(namespace=namespace, name=f"ingress-{uid}")
        except ApiException as e:
            print("Resource does not exist!")

        try:
            core_v1_api.delete_namespaced_service(namespace=namespace, name=f"service-{uid}")
        except ApiException as e:
            print("Resource does not exist!")

        try:
            apps_v1_api.delete_namespaced_deployment(namespace=namespace, name=f"deployment-{uid}")
        except ApiException as e:
            print("Resource does not exist!")

        try:
            core_v1_api.delete_namespaced_secret(namespace=namespace, name=f"secret-{uid}")
        except ApiException as e:
            print("Resource does not exist!")

        return JSONResponse(content="Could not make notebook!", status_code=500)

    session = Session()

    new_record = MyTable(user_id=user_id, notebook_id=uid, done=False, created_at=datetime.datetime.now())
    session.add(new_record)
    session.commit()

    session.close()

    return_data = {
        "notebook_id": uid,
        "notebook_password": password
    }

    return JSONResponse(content=return_data, status_code=201)


@app.get("/get_notebook_details")
async def get_notebook_details(user_id: str):
    config.load_incluster_config()

    apps_v1_api = client.AppsV1Api()

    session = Session()

    instances = session.query(MyTable).filter(MyTable.user_id == user_id).all()

    return_data = []

    for instance in instances:
        notebook_id = instance.notebook_id
        try:
            deployment = apps_v1_api.read_namespaced_deployment(f"deployment-{notebook_id}", namespace=namespace)

            creation_timestamp = deployment.metadata.creation_timestamp
            format_creation_timestamp = creation_timestamp.strftime("%Y:%m:%d %H:%M:%S")

            expiration_time = creation_timestamp + datetime.timedelta(days=10)
            format_expiration_timestamp = expiration_time.strftime("%Y:%m:%d %H:%M:%S")

        except client.exceptions.ApiException as e:
            return JSONResponse(content="Exception when calling Kubernetes API: %s\n" % e, status_code=500)

        data = {
            "notebook_id": notebook_id,
            "creation_time": format_creation_timestamp,
            "expiration_time": format_expiration_timestamp
        }

        return_data.append(data)

    session.close()

    return JSONResponse(content=return_data, status_code=200)


if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0')