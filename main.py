import os
import uvicorn
from uuid import uuid4
from fastapi import FastAPI, Header
import datetime
import requests
from dotenv import load_dotenv
from pod_utils import create_service, create_deployment, create_secret, create_ingress
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from starlette.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import create_engine, Column, String, DateTime

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
    last_accessed = Column(DateTime)
    created_at = Column(DateTime)
    description = Column(String)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["localhost"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)


@app.get("/main_api")
async def connection_test():
    return JSONResponse("Server Works!", status_code=200)


@app.put("/main_api/create_notebook_instance")
async def create_notebook_instance(user_id: str, description: str, dataset_url: str, authorization: str = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]

        response = requests.get(os.getenv("KEYCLOAK_URL"), headers={"Authorization": f"Bearer {token}"})

        if response.status_code == 200:
            config.load_incluster_config()
            apps_v1_api = client.AppsV1Api()
            core_v1_api = client.CoreV1Api()
            networking_v1_api = client.NetworkingV1Api()

            uid = str(uuid4())
            deployment_body = create_deployment(uid)
            service_body, service_port = create_service(uid, namespace=namespace)
            secret_body, password = create_secret(uid, dataset_url)
            ingress_body = create_ingress(uid, service_port)
            if service_body is None:
                return JSONResponse(content="Could not deploy a new instance!", status_code=500)
            try:
                core_v1_api.create_namespaced_secret(namespace=namespace, body=secret_body)
                apps_v1_api.create_namespaced_deployment(namespace=namespace, body=deployment_body)
                core_v1_api.create_namespaced_service(namespace=namespace, body=service_body)
                networking_v1_api.create_namespaced_ingress(namespace=namespace, body=ingress_body)
            except ApiException as e:
                print(f"Error creating pod {e}")
                try:
                    networking_v1_api.delete_namespaced_ingress(namespace=namespace, name=f"ingress-{uid}")
                except ApiException as e:
                    print(f"Resource does not exist! {e}")

                try:
                    core_v1_api.delete_namespaced_service(namespace=namespace, name=f"service-{uid}")
                except ApiException as e:
                    print(f"Resource does not exist! {e}")

                try:
                    apps_v1_api.delete_namespaced_deployment(namespace=namespace, name=f"deployment-{uid}")
                except ApiException as e:
                    print(f"Resource does not exist! {e}")

                try:
                    core_v1_api.delete_namespaced_secret(namespace=namespace, name=f"secret-{uid}")
                except ApiException as e:
                    print(f"Resource does not exist! {e}")

                return JSONResponse(content="Could not make notebook!", status_code=500)

            session = Session()

            new_record = MyTable(user_id=user_id, notebook_id=uid, last_accessed=datetime.datetime.now(),
                                 created_at=datetime.datetime.now(), description=description)
            session.add(new_record)
            session.commit()

            session.close()

            return_data = {
                "notebook_id": uid,
                "notebook_password": password
            }

            return JSONResponse(content=return_data, status_code=201)
        else:
            return JSONResponse(content="Unauthorized access!", status_code=401)
    else:
        return JSONResponse(content="Authorization token not provided!", status_code=400)


@app.get("/main_api/get_notebook_details")
async def get_notebook_details(user_id: str, authorization: str = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]

        response = requests.get(os.getenv("KEYCLOAK_URL"), headers={"Authorization": f"Bearer {token}"})

        if response.status_code == 200:
            config.load_incluster_config()

            apps_v1_api = client.AppsV1Api()

            session = Session()

            instances = session.query(MyTable).filter(MyTable.user_id == user_id).all()

            return_data = []

            for instance in instances:
                notebook_id = instance.notebook_id
                try:
                    deployment = apps_v1_api.read_namespaced_deployment(f"deployment-{notebook_id}",
                                                                        namespace=namespace)

                    creation_timestamp = deployment.metadata.creation_timestamp
                    format_creation_timestamp = creation_timestamp.isoformat()

                    expiration_time = creation_timestamp + datetime.timedelta(days=10)
                    format_expiration_timestamp = expiration_time.isoformat()

                except client.exceptions.ApiException as e:
                    return JSONResponse(content="Exception when calling Kubernetes API: %s\n" % e, status_code=500)

                data = {
                    "notebook_id": notebook_id,
                    "creation_time": format_creation_timestamp,
                    "expiration_time": format_expiration_timestamp,
                    "last_accessed": instance.last_accessed,
                    "description": instance.description
                }

                return_data.append(data)

            session.close()

            return JSONResponse(content=return_data, status_code=200)
        else:
            return JSONResponse(content="Unauthorized access!", status_code=401)
    else:
        return JSONResponse(content="Authorization token not provided!", status_code=400)


@app.post("/main_api/update_access")
async def update_access(uid: str, authorization: str = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]

        response = requests.get(os.getenv("KEYCLOAK_URL"), headers={"Authorization": f"Bearer {token}"})

        if response.status_code == 200:
            session = Session()

            result = session.query(MyTable).filter(MyTable.notebook_id == uid).all()
            result[0].last_accessed = datetime.datetime.now()

            session.commit()
            session.close()
            return JSONResponse(content="Update Access Successfully!", status_code=200)
        else:
            return JSONResponse(content="Unauthorized access!", status_code=401)
    else:
        return JSONResponse(content="Authorization token not provided!", status_code=400)


@app.delete("main_api/delete_notebook")
async def delete_notebook(uid: str, authorization: str = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]

        response = requests.get(os.getenv("KEYCLOAK_URL"), headers={"Authorization": f"Bearer {token}"})

        if response.status_code == 200:
            config.load_incluster_config()
            apps_v1_api = client.AppsV1Api()
            core_v1_api = client.CoreV1Api()
            networking_v1_api = client.NetworkingV1Api()

            session = Session()

            row_to_delete = session.query(MyTable).filter(MyTable.notebook_id == uid).first()

            if row_to_delete:
                session.delete(row_to_delete)
                session.commit()
            else:
                return JSONResponse(content="Failed to delete notebook!", status_code=500)

            session.close()

            try:
                networking_v1_api.delete_namespaced_ingress(namespace=namespace, name=f"ingress-{uid}")
            except ApiException as e:
                print(f"Resource does not exist! {e}")

            try:
                core_v1_api.delete_namespaced_service(namespace=namespace, name=f"service-{uid}")
            except ApiException as e:
                print(f"Resource does not exist! {e}")

            try:
                apps_v1_api.delete_namespaced_deployment(namespace=namespace, name=f"deployment-{uid}")
            except ApiException as e:
                print(f"Resource does not exist! {e}")

            try:
                core_v1_api.delete_namespaced_secret(namespace=namespace, name=f"secret-{uid}")
            except ApiException as e:
                print(f"Resource does not exist! {e}")

            return JSONResponse(content="Deleted Notebook Successfully!", status_code=200)
        else:
            return JSONResponse(content="Unauthorized access!", status_code=401)
    else:
        return JSONResponse(content="Authorization token not provided!", status_code=400)

if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0')
