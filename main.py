import json
import logging
import os
import uvicorn
from uuid import uuid4
from fastapi import FastAPI, Header
import datetime
import requests
from pydantic import BaseModel
from pod_utils import create_ingress, create_service, create_deployment, create_secret
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from starlette.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import create_engine, Column, String, DateTime, Integer
from redis_cache import is_data_stale, get_data_from_redis, set_data_in_redis, update_timestamp

app = FastAPI(docs_url="/notebook_manager/docs")
Base = declarative_base()
namespace = os.getenv("NAMESPACE")
password = os.getenv("POSTGRES_PASSWORD").strip().replace("\n", "")
engine = create_engine(f'postgresql+psycopg2://'
                       f'{os.getenv("POSTGRES_USER")}:{password}@{os.getenv("POSTGRES_HOST")}'
                       f':{os.getenv("POSTGRES_PORT")}/{os.getenv("POSTGRES_DB")}')
Session = sessionmaker(bind=engine)


class MyTable(Base):
    __tablename__ = "notebooks"
    notebook_id = Column(String, primary_key=True)
    user_id = Column(String)
    last_accessed = Column(DateTime)
    created_at = Column(DateTime)
    description = Column(String)
    port = Column(Integer)
    notebook_type = Column(String)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


class NotebookInstance(BaseModel):
    user_id: str
    description: str
    dataset_url: str
    notebook_type: str

@app.get("/notebook_manager")
async def connection_test():
    return JSONResponse("Server Works!", status_code=200)


@app.post("/notebook_manager/create_notebook_instance", tags=["POST"])
async def create_notebook_instance(notebook_instance: NotebookInstance, authorization: str = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]

        response = requests.get(os.getenv("KEYCLOAK_URL"), headers={"Authorization": f"Bearer {token}"}, verify=False)

        if response.status_code == 200:
            config.load_incluster_config()
            apps_v1_api = client.AppsV1Api()
            core_v1_api = client.CoreV1Api()
            networking_v1_api = client.NetworkingV1Api()

            if notebook_instance.notebook_type not in ["sklearn", "pytorch"]:
                return JSONResponse(status_code=400, content="Notebook Type can be only sklearn or pytorch!")

            uid = str(uuid4())
            deployment_body = create_deployment(uid, notebook_type=notebook_instance.notebook_type)
            service_body, service_port = create_service(uid, namespace=namespace)
            secret_body = create_secret(uid, notebook_instance.dataset_url, notebook_instance.user_id)
            ingress_body = create_ingress(uid, service_port)
            if service_body is None:
                return JSONResponse(content="Could not deploy a new instance!", status_code=500)
            try:
                core_v1_api.create_namespaced_secret(namespace=namespace, body=secret_body)
                apps_v1_api.create_namespaced_deployment(namespace=namespace, body=deployment_body)
                core_v1_api.create_namespaced_service(namespace=namespace, body=service_body)
                networking_v1_api.create_namespaced_ingress(namespace=namespace, body=ingress_body)
            except ApiException as e:
                logging.error(f"Error creating pod {e}")

                try:
                    networking_v1_api.delete_namespaced_ingress(namespace=namespace, name=f"ingress-{uid}")
                    core_v1_api.delete_namespaced_service(namespace=namespace, name=f"service-{uid}")
                    apps_v1_api.delete_namespaced_deployment(namespace=namespace, name=f"deployment-{uid}")
                    core_v1_api.delete_namespaced_secret(namespace=namespace, name=f"secret-{uid}")
                    logging.info("Successfully deleted all components")
                except ApiException as e:
                    logging.error("Could not delete some component!")

                    return JSONResponse(content="Could not make notebook!", status_code=500)

            session = Session()

            new_record = MyTable(user_id=notebook_instance.user_id, notebook_id=uid, last_accessed=datetime.datetime.now(),
                                 created_at=datetime.datetime.now(), description=notebook_instance.description, port=service_port, notebook_type=notebook_instance.notebook_type)
            session.add(new_record)
            session.commit()

            session.close()

            return_data = {
                "notebook_id": uid
            }

            return JSONResponse(content=return_data, status_code=201)
        else:
            return JSONResponse(content="Unauthorized access!", status_code=401)
    else:
        return JSONResponse(content="Authorization token not provided!", status_code=400)


@app.get("/notebook_manager/get_notebook_details", tags=["GET"])
async def get_notebook_details(user_id: str, authorization: str = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]

        response = requests.get(os.getenv("KEYCLOAK_URL"), headers={"Authorization": f"Bearer {token}"}, verify=False)

        if response.status_code != 200:
            return JSONResponse(content="Unauthorized access!", status_code=401)

        cache_key = f"user_{user_id}_notebook_details"
        if not is_data_stale(cache_key, 3600):  # assuming you want a 1-hour expiration
            cached_data = json.loads(get_data_from_redis(cache_key))
            if cached_data:
                return JSONResponse(content=cached_data, status_code=200)

        config.load_incluster_config()

        apps_v1_api = client.AppsV1Api()

        session = Session()

        try:
            instances = session.query(MyTable).filter(MyTable.user_id == user_id).all()

            return_data = []

            for instance in instances:
                notebook_id = instance.notebook_id
                try:
                    deployment = apps_v1_api.read_namespaced_deployment(f"deployment-{notebook_id}",
                                                                        namespace=namespace)

                    creation_timestamp = deployment.metadata.creation_timestamp
                    format_creation_timestamp = creation_timestamp.strftime("%m/%d/%Y")

                    expiration_time = creation_timestamp + datetime.timedelta(days=10)
                    format_expiration_timestamp = expiration_time.strftime("%m/%d/%Y")

                except client.exceptions.ApiException as e:
                    return JSONResponse(content="Exception when calling Kubernetes API: %s\n" % e, status_code=500)

                data = {
                    "notebook_id": notebook_id,
                    "creation_time": format_creation_timestamp,
                    "expiration_time": format_expiration_timestamp,
                    "last_accessed": instance.last_accessed.strftime("%m/%d/%Y"),
                    "description": instance.description,
                    "port": instance.port,
                    "notebook_type": instance.notebook_type
                }
                return_data.append(data)

            set_data_in_redis(cache_key, json.dumps(return_data), 3600)
            update_timestamp(cache_key)

            return JSONResponse(content=return_data, status_code=200)

        except client.exceptions.ApiException as e:
            return JSONResponse(content=f"Exception when calling Kubernetes API: {e}", status_code=500)
        finally:
            session.close()
    else:
        return JSONResponse(content="Authorization token not provided!", status_code=400)


@app.put("/notebook_manager/update_access", tags=["PUT"])
async def update_access(uid: str, authorization: str = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]

        response = requests.get(os.getenv("KEYCLOAK_URL"), headers={"Authorization": f"Bearer {token}"}, verify=False)

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


@app.delete("/notebook_manager/delete_notebook", tags=["DELETE"])
async def delete_notebook(uid: str, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        return JSONResponse(status_code=400, content="Authorization token not provided or invalid")

    token = authorization.split(" ")[1]
    response = requests.get(os.getenv("KEYCLOAK_URL"), headers={"Authorization": f"Bearer {token}"}, verify=False)

    if response.status_code != 200:
        return JSONResponse(status_code=401, content="Unauthorized access")

    config.load_incluster_config()
    apps_v1_api = client.AppsV1Api()
    core_v1_api = client.CoreV1Api()
    networking_v1_api = client.NetworkingV1Api()

    with Session() as session:
        row_to_delete = session.query(MyTable).filter(MyTable.notebook_id == uid).first()

        if not row_to_delete:
            return JSONResponse(status_code=404, content="Notebook not found")

        session.delete(row_to_delete)
        session.commit()

    try:
        networking_v1_api.delete_namespaced_ingress(namespace=namespace, name=f"ingress-{uid}")
        core_v1_api.delete_namespaced_service(namespace=namespace, name=f"service-{uid}")
        apps_v1_api.delete_namespaced_deployment(namespace=namespace, name=f"deployment-{uid}")
        core_v1_api.delete_namespaced_secret(namespace=namespace, name=f"secret-{uid}")
    except client.exceptions.ApiException as e:
        logging.error(f"Kubernetes resource deletion error: {e}")
        return JSONResponse(status_code=500, detail="Error occurred while deleting Kubernetes resources")

    return JSONResponse(content="Deleted Notebook Successfully", status_code=200)

if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0')
