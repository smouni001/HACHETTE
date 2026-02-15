# Deploiement AWS ECS Fargate (pas a pas)

Ce guide deploie l'app web FastAPI dans ECS Fargate via ECR.

## 0) Prerequis

- AWS account
- AWS CLI v2 configure (`aws configure`)
- Docker Desktop
- Region exemple: `eu-west-3` (Paris)

Variables a definir dans PowerShell:

```powershell
$env:AWS_REGION="eu-west-3"
$env:AWS_ACCOUNT_ID=(aws sts get-caller-identity --query Account --output text)
$env:APP_NAME="idil-papyrus-web"
$env:ECR_REPO="$env:APP_NAME"
```

## 1) Build et push de l'image dans ECR

Creer le repository ECR:

```powershell
aws ecr create-repository --repository-name $env:ECR_REPO --region $env:AWS_REGION
```

Login Docker sur ECR:

```powershell
aws ecr get-login-password --region $env:AWS_REGION | docker login --username AWS --password-stdin "$env:AWS_ACCOUNT_ID.dkr.ecr.$env:AWS_REGION.amazonaws.com"
```

Build et tag:

```powershell
docker build -t $env:APP_NAME .
docker tag $env:APP_NAME`:latest "$env:AWS_ACCOUNT_ID.dkr.ecr.$env:AWS_REGION.amazonaws.com/$env:ECR_REPO`:latest"
```

Push:

```powershell
docker push "$env:AWS_ACCOUNT_ID.dkr.ecr.$env:AWS_REGION.amazonaws.com/$env:ECR_REPO`:latest"
```

## 2) Creer un cluster ECS Fargate

```powershell
aws ecs create-cluster --cluster-name "$env:APP_NAME-cluster" --region $env:AWS_REGION
```

## 3) IAM roles ECS (execution + task)

Role d'execution (pull image ECR + logs CloudWatch):

```powershell
aws iam create-role `
  --role-name "$env:APP_NAME-ecs-exec-role" `
  --assume-role-policy-document file://web_app/deploy/ecs-task-trust-policy.json

aws iam attach-role-policy `
  --role-name "$env:APP_NAME-ecs-exec-role" `
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
```

Role task (optionnel pour acces S3/Secrets Manager):

```powershell
aws iam create-role `
  --role-name "$env:APP_NAME-ecs-task-role" `
  --assume-role-policy-document file://web_app/deploy/ecs-task-trust-policy.json
```

## 4) Enregistrer la task definition

1. Generez la task definition avec le script:

```powershell
.\web_app\deploy\render-taskdef.ps1 -Region $env:AWS_REGION -AppName $env:APP_NAME
```

2. Enregistrez:

```powershell
aws ecs register-task-definition `
  --cli-input-json file://web_app/deploy/taskdef.rendered.json `
  --region $env:AWS_REGION
```

## 5) Creer service ECS + load balancer

Le plus simple est de passer par la console AWS:

1. ECS > Cluster > Create service
2. Launch type: Fargate
3. Task definition: `idil-papyrus-web`
4. Desired tasks: `1`
5. Load balancer: Application Load Balancer
6. Listener HTTP 80 -> target group port 8000
7. Security group: autoriser 80 (et 443 si HTTPS via ACM)

## 6) Variables d'environnement a definir dans la task

Dans la task definition:

- `IDP470_WEB_SOURCE=/app/IDP470RA.pli`
- `IDP470_WEB_SPEC_PDF=/app/2785 - DOCTECHN - Dilifac - Format IDIL.pdf`
- `IDP470_WEB_LOGO=/app/assets/logo_hachette_livre.png`
- `IDP470_WEB_JOBS_DIR=/app/web_app/jobs`
- `IDP470_WEB_INPUT_ENCODING=latin-1`
- `IDP470_WEB_CONTINUE_ON_ERROR=false`
- `UVICORN_WORKERS=2`

## 7) Stockage persistant (fortement recommande)

Par defaut, Fargate est ephemere. Pour conserver les jobs et fichiers:

- Option A: monter un volume EFS sur `/app/web_app/jobs`
- Option B: evoluer vers stockage S3 (recommande long terme)

## 8) Verification

Une fois le service `RUNNING`:

- Ouvrir l'URL de l'ALB
- Tester `/api/health`
- Charger un FACDEMA depuis l'UI
- Verifier telechargements Excel/PDF

## 9) Mise a jour d'image

1. Rebuild + push image `:latest`
2. Force new deployment ECS:

```powershell
aws ecs update-service `
  --cluster "$env:APP_NAME-cluster" `
  --service "$env:APP_NAME-service" `
  --force-new-deployment `
  --region $env:AWS_REGION
```
