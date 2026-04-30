# Coworking Space Analytics Service

A Flask-based microservice that provides analytics on coworking space usage — daily check-ins and per-user visit history. Deployed as a containerized workload on Kubernetes, backed by PostgreSQL.

---

## Architecture Overview

```
                      ┌─────────────────────────────────────────┐
                      │              Kubernetes Cluster          │
                      │                                          │
  Client / curl  ───► │  [Service: LoadBalancer :5153]          │
                      │          │                               │
                      │  [Deployment: coworking]                 │
                      │    Flask app (port 5153)                 │
                      │          │                               │
                      │  [Service: postgresql-service :5432]     │
                      │          │                               │
                      │  [Deployment: postgresql]  ◄── [PVC]    │
                      └─────────────────────────────────────────┘
```

**CI/CD (production path)**

```
GitHub push ──► AWS CodeBuild ──► Docker image ──► AWS ECR ──► kubectl apply ──► EKS
```

**Key components:**

| Component | Role |
|---|---|
| `analytics/app.py` | Flask app — 4 routes + APScheduler background job |
| `analytics/config.py` | DB connection string assembled from env vars |
| `db/*.sql` | Schema creation + seed data |
| `deployments/` | PostgreSQL PVC, Deployment, Service |
| `deployment-local/` | ConfigMap, Secret, app Deployment+Service for local k8s |
| `deployment/` | Same manifests pointing at an ECR image (production) |

---

## Prerequisites

### Local development

| Tool | Minimum version | Purpose |
|---|---|---|
| Python | 3.6+ | Run app locally without Docker |
| Docker | any recent | Build and run images |
| kubectl | 1.24+ | Apply k8s manifests |
| minikube **or** kind | any | Local Kubernetes cluster |
| helm | 3.x | Optional — Bitnami PostgreSQL chart |
| psql | any | Seed the database |

### Production (AWS)

| Service | Purpose |
|---|---|
| AWS EKS | Managed Kubernetes cluster |
| AWS ECR | Docker image registry |
| AWS CodeBuild | CI/CD — builds and pushes the image |
| AWS CloudWatch | Log aggregation for the EKS workloads |
| AWS IAM | Roles for CodeBuild → ECR and EKS node → CloudWatch |

---

## Path 1 — Local Kubernetes (minikube / kind)

Use this path for development and testing without AWS.

### Step 1 — Start a local cluster

```bash
minikube start
# OR
kind create cluster
```

### Step 2 — Deploy PostgreSQL

The `deployments/` directory contains a plain-Kubernetes PostgreSQL setup (no Helm required).

```bash
kubectl apply -f deployments/pv.yaml
kubectl apply -f deployments/pvc.yaml
kubectl apply -f deployments/postgresql-deployment.yaml
kubectl apply -f deployments/postgresql-service.yaml
```

Wait for the pod to be ready:

```bash
kubectl wait --for=condition=ready pod -l app=postgresql --timeout=60s
```

The PostgreSQL instance runs with these defaults (defined in `deployments/postgresql-deployment.yaml`):

| Variable | Value |
|---|---|
| `POSTGRES_DB` | `mydatabase` |
| `POSTGRES_USER` | `myuser` |
| `POSTGRES_PASSWORD` | `mypassword` |

> To use different credentials, edit `deployments/postgresql-deployment.yaml` **and** the ConfigMap/Secret in the next steps.

### Step 3 — Seed the database

Forward the PostgreSQL port to your local machine, then run the SQL files in order:

```bash
kubectl port-forward svc/postgresql-service 5432:5432 &

PGPASSWORD=mypassword psql -h 127.0.0.1 -U myuser -d mydatabase -f db/1_create_tables.sql
PGPASSWORD=mypassword psql -h 127.0.0.1 -U myuser -d mydatabase -f db/2_seed_users.sql
PGPASSWORD=mypassword psql -h 127.0.0.1 -U myuser -d mydatabase -f db/3_seed_tokens.sql
```

Kill the port-forward once done:

```bash
kill %1
```

### Step 4 — Build the Docker image

> You need a `Dockerfile` at the project root. A minimal one that works with this app:
>
> ```dockerfile
> FROM python:3.11-slim
> WORKDIR /app
> COPY analytics/requirements.txt .
> RUN pip install --no-cache-dir -r requirements.txt
> COPY analytics/ .
> EXPOSE 5153
> CMD ["python", "app.py"]
> ```

Build the image and make it available inside the cluster:

```bash
# With minikube — build directly inside minikube's Docker daemon
eval $(minikube docker-env)
docker build -t coworking:1.0.0 .

# With kind — build normally then load into kind
docker build -t coworking:1.0.0 .
kind load docker-image coworking:1.0.0
```

Use semantic versioning (`MAJOR.MINOR.PATCH`) for every image you build.

### Step 5 — Configure ConfigMap and Secret

Edit `deployment-local/configmap.yaml` and fill in the placeholder values:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: coworking-config
data:
  DB_NAME: mydatabase
  DB_USERNAME: myuser
  DB_HOST: postgresql-service   # matches the k8s Service name
  DB_PORT: "5432"
---
apiVersion: v1
kind: Secret
metadata:
  name: coworking-secret
type: Opaque
data:
  DB_PASSWORD: bXlwYXNzd29yZA==   # base64 of "mypassword"
```

To encode a password:

```bash
echo -n 'mypassword' | base64
```

Apply the config:

```bash
kubectl apply -f deployment-local/configmap.yaml
```

### Step 6 — Update the deployment manifest

Edit `deployment-local/coworking.yaml` and replace the placeholder values:

```yaml
image: coworking:1.0.0          # image you built in Step 4
# ...
envFrom:
  - configMapRef:
      name: coworking-config    # name from configmap.yaml
env:
  - name: DB_PASSWORD
    valueFrom:
      secretKeyRef:
        name: coworking-secret  # name from configmap.yaml
        key: DB_PASSWORD        # key in the Secret
```

### Step 7 — Deploy the application

```bash
kubectl apply -f deployment-local/coworking.yaml
kubectl rollout status deployment/coworking
```

### Step 8 — Test the API

```bash
# Get the NodePort assigned
NODE_PORT=$(kubectl get svc coworking -o jsonpath='{.spec.ports[0].nodePort}')
MINIKUBE_IP=$(minikube ip)   # omit for kind; use localhost

curl http://$MINIKUBE_IP:$NODE_PORT/health_check
curl http://$MINIKUBE_IP:$NODE_PORT/api/reports/daily_usage
curl http://$MINIKUBE_IP:$NODE_PORT/api/reports/user_visits
```

---

## Path 2 — Production on AWS EKS

Use this path for a production-grade deployment with a CI/CD pipeline.

### Step 1 — Provision an EKS cluster

Create or connect to an existing EKS cluster, then update your local kubeconfig:

```bash
aws eks update-kubeconfig --region <REGION> --name <CLUSTER_NAME>
kubectl get nodes   # verify connectivity
```

**Recommended node type:** `t3.medium` (2 vCPU, 4 GB RAM). The Flask app itself is lightweight; the headroom is reserved for the PostgreSQL sidecar and the CloudWatch agent DaemonSet. For cost-optimised setups, use a managed node group with Spot Instances for non-critical workloads and keep PostgreSQL on an On-Demand node.

### Step 2 — Create an ECR repository

```bash
aws ecr create-repository --repository-name coworking --region <REGION>
```

Note the repository URI — you'll use it as the image reference in the deployment manifest.

### Step 3 — Set up AWS CodeBuild

Create a CodeBuild project with:

- **Source**: your GitHub repository
- **Environment**: managed image, Amazon Linux 2, privileged mode (required for Docker)
- **Service role**: a role with `AmazonEC2ContainerRegistryPowerUser` + `AmazonEKSClusterPolicy`

Your `buildspec.yml` should:

1. Log in to ECR
2. Build the image with a semantic version tag (e.g., from `$CODEBUILD_RESOLVED_SOURCE_VERSION`)
3. Push to ECR
4. Optionally trigger `kubectl set image` or run `kubectl apply` with the new image URI

Minimal `buildspec.yml` skeleton:

```yaml
version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $ECR_REGISTRY
  build:
    commands:
      - docker build -t $ECR_REGISTRY/coworking:$IMAGE_TAG .
      - docker push $ECR_REGISTRY/coworking:$IMAGE_TAG
  post_build:
    commands:
      - aws eks update-kubeconfig --region $AWS_DEFAULT_REGION --name $EKS_CLUSTER_NAME
      - kubectl set image deployment/coworking coworking=$ECR_REGISTRY/coworking:$IMAGE_TAG
```

Set `ECR_REGISTRY`, `EKS_CLUSTER_NAME`, and `IMAGE_TAG` as CodeBuild environment variables.

### Step 4 — Install the EBS CSI driver

EKS does not ship with the EBS CSI driver by default. It must be installed before any PVC backed by `gp2`/`gp3` storage can be provisioned — **no manual PV creation is needed once the driver is in place**.

```bash
# 1. Associate the OIDC provider with the cluster (required for IRSA)
eksctl utils associate-iam-oidc-provider \
  --cluster coworking-cluster \
  --region us-east-1 \
  --approve

# 2. Create an IAM service account and the backing IAM role
eksctl create iamserviceaccount \
  --name ebs-csi-controller-sa \
  --namespace kube-system \
  --cluster coworking-cluster \
  --region us-east-1 \
  --attach-policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy \
  --approve \
  --role-only \
  --role-name AmazonEKS_EBS_CSI_DriverRole

# 3. Install the add-on — replace the account ID in the role ARN with yours
aws eks create-addon \
  --cluster-name coworking-cluster \
  --addon-name aws-ebs-csi-driver \
  --region us-east-1 \
  --service-account-role-arn arn:aws:iam::<ACCOUNT_ID>:role/AmazonEKS_EBS_CSI_DriverRole

# 4. Wait until the add-on reports ACTIVE before continuing
aws eks describe-addon \
  --cluster-name coworking-cluster \
  --addon-name aws-ebs-csi-driver \
  --region us-east-1 \
  --query "addon.status"
```

The driver dynamically provisions an EBS volume when a PVC is created, so there is no need to pre-create a PersistentVolume.

### Step 5 — Deploy PostgreSQL to EKS

```bash
kubectl apply -f deployments/pvc.yaml
kubectl apply -f deployments/postgresql-deployment.yaml
kubectl apply -f deployments/postgresql-service.yaml
kubectl wait --for=condition=ready pod -l app=postgresql --timeout=90s
```

### Step 6 — Seed the database

```bash
kubectl port-forward svc/postgresql-service 5432:5432 &

PGPASSWORD=mypassword psql -h 127.0.0.1 -U myuser -d mydatabase -f db/1_create_tables.sql
PGPASSWORD=mypassword psql -h 127.0.0.1 -U myuser -d mydatabase -f db/2_seed_users.sql
PGPASSWORD=mypassword psql -h 127.0.0.1 -U myuser -d mydatabase -f db/3_seed_tokens.sql

kill %1
```

### Step 7 — Configure ConfigMap and Secret

Fill in `deployment/configmap.yaml`:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: coworking-config
data:
  DB_NAME: mydatabase
  DB_USERNAME: myuser
  DB_HOST: postgresql-service
  DB_PORT: "5432"
---
apiVersion: v1
kind: Secret
metadata:
  name: coworking-secret
type: Opaque
data:
  DB_PASSWORD: <base64-encoded-password>
```

Apply:

```bash
kubectl apply -f deployment/configmap.yaml
```

### Step 8 — Deploy the application

Fill in `deployment/coworking.yaml` — replace `<DOCKER_IMAGE_URI_FROM_ECR>` with the full ECR image URI (including tag), and update the ConfigMap/Secret name references to match Step 7.

```bash
kubectl apply -f deployment/coworking.yaml
kubectl rollout status deployment/coworking
```

### Step 9 — Verify

```bash
kubectl get svc coworking                        # note the EXTERNAL-IP from LoadBalancer
kubectl get pods
kubectl describe deployment coworking
kubectl logs -l service=coworking --tail=50
```

Once `EXTERNAL-IP` is assigned:

```bash
EXTERNAL_IP=$(kubectl get svc coworking -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
curl http://$EXTERNAL_IP:5153/health_check
curl http://$EXTERNAL_IP:5153/api/reports/daily_usage
```

### Step 10 — Enable CloudWatch logging

Install the CloudWatch Observability add-on on your EKS cluster (AWS Console → EKS → Add-ons, or via CLI):

```bash
aws eks create-addon \
  --cluster-name <CLUSTER_NAME> \
  --addon-name amazon-cloudwatch-observability \
  --region <REGION>
```

Application logs emitted to stdout/stderr by Flask (via `app.logger`) are automatically shipped to CloudWatch under the log group `/aws/containerinsights/<CLUSTER_NAME>/application`.

---

## Releasing Updates

The deployment process for a new version of the application:

1. **Make your code change** in `analytics/`.
2. **Build and push a new image** with an incremented semantic version tag:
   ```bash
   docker build -t <ECR_URI>/coworking:1.1.0 .
   docker push <ECR_URI>/coworking:1.1.0
   ```
   If using CodeBuild, a push to the configured branch triggers this automatically.
3. **Update the running deployment** (zero-downtime rolling update):
   ```bash
   kubectl set image deployment/coworking coworking=<ECR_URI>/coworking:1.1.0
   kubectl rollout status deployment/coworking
   ```
   Kubernetes replaces pods one at a time, waiting for liveness and readiness probes to pass before terminating the old pod.
4. **Roll back** if something goes wrong:
   ```bash
   kubectl rollout undo deployment/coworking
   ```
5. **Update this README** if the deployment process or configuration changes.

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health_check` | Liveness check — returns `ok` |
| GET | `/readiness_check` | Readiness check — queries the `tokens` table |
| GET | `/api/reports/daily_usage` | Daily check-in counts grouped by date |
| GET | `/api/reports/user_visits` | Visit count + join date per user |

The app also runs a background job via APScheduler that recomputes daily visits every 30 seconds and logs the result.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DB_USERNAME` | yes | — | PostgreSQL user |
| `DB_PASSWORD` | yes | — | PostgreSQL password |
| `DB_HOST` | no | `127.0.0.1` | PostgreSQL host |
| `DB_PORT` | no | `5432` | PostgreSQL port |
| `DB_NAME` | no | `postgres` | PostgreSQL database name |
| `APP_PORT` | no | `5153` | Flask listen port |

In Kubernetes, `DB_USERNAME`, `DB_HOST`, `DB_PORT`, and `DB_NAME` come from the ConfigMap; `DB_PASSWORD` comes from a Secret.

---

## Cost Optimisation Notes

- **Node type**: `t3.medium` is sufficient for this workload. If traffic is bursty, consider `t3.small` with a Horizontal Pod Autoscaler on the Flask deployment.
- **Spot Instances**: Use a mixed node group (On-Demand for PostgreSQL, Spot for the analytics service). The app is stateless and tolerates restarts — a good Spot candidate.
- **Single replica**: The current deployment runs one replica. A second replica adds fault tolerance for the Flask tier without major cost impact, since the bottleneck is the single PostgreSQL pod.
- **EBS volume**: The 1 Gi `gp2` PVC for PostgreSQL is minimal. Right-size it based on actual data growth; `gp3` is cheaper and faster than `gp2` at equivalent sizes.
- **CloudWatch**: Log retention defaults to never expire — set a retention policy (e.g., 30 days) to avoid unbounded log storage costs.

---

## Troubleshooting

**Pods stuck in `CrashLoopBackOff`**

```bash
kubectl logs <POD_NAME> --previous
```

Most common causes: missing or incorrect env vars (check ConfigMap/Secret names match the deployment), or the database is not reachable (check `postgresql-service` is running).

**`readiness_check` returns 500**

The app cannot reach PostgreSQL. Verify the `DB_HOST` in the ConfigMap matches the Kubernetes Service name (`postgresql-service`), and that the PostgreSQL pod is healthy:

```bash
kubectl get pods -l app=postgresql
```

**Port-forward disconnects during seeding**

Large SQL files (like `3_seed_tokens.sql` at 329 KB) can time out. Run port-forward in a separate terminal or use `nohup`.

**PVC stays in `Pending` on EKS**

The EBS CSI driver is not installed or the OIDC provider is not associated. Follow Path 2 → Step 4 in full, then re-apply the PVC. You can check driver status with:

```bash
aws eks describe-addon \
  --cluster-name coworking-cluster \
  --addon-name aws-ebs-csi-driver \
  --region us-east-1 \
  --query "addon.status"
```
