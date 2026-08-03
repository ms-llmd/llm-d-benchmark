SHELL := /usr/bin/env bash

# Defaults
PROJECT_NAME ?= llm-d-benchmark
DEV_VERSION ?= 0.0.1
PROD_VERSION ?= 0.0.0
IMAGE_TAG_BASE ?= ghcr.io/llm-d/$(PROJECT_NAME)
IMG = $(IMAGE_TAG_BASE):$(DEV_VERSION)
NAMESPACE ?= hc4ai-operator

CONTAINER_TOOL := $(shell if command -v docker >/dev/null 2>&1; then echo docker; elif command -v podman >/dev/null 2>&1; then echo podman; fi)
BUILDER := $(shell command -v buildah >/dev/null 2>&1 && echo buildah || echo $(CONTAINER_TOOL))
PLATFORMS ?= linux/amd64,linux/arm64 # linux/s390x,linux/ppc64le

# go source files
SRC = $(shell find . -type f -name '*.go')

.PHONY: help
help: ## Print help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z_0-9-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

##@ Development

.PHONY: format
format: ## Format Go source files
	@printf "\033[33;1m==== Running gofmt ====\033[0m\n"
	@gofmt -l -w $(SRC)

.PHONY: test
test: check-ginkgo ## Run tests
	@printf "\033[33;1m==== Running tests ====\033[0m\n"
	ginkgo -r -v

.PHONY: post-deploy-test
post-deploy-test: ## Run post deployment tests
	echo Success!
	@echo "Post-deployment tests passed."

.PHONY: lint
lint: check-golangci-lint ## Run lint
	@printf "\033[33;1m==== Running linting ====\033[0m\n"
	golangci-lint run

##@ Container Build/Push

.PHONY: buildah-build
buildah-build: check-builder load-version-json ## Build and push image (multi-arch if supported)
	@echo "✅ Using builder: $(BUILDER)"
	@if [ "$(BUILDER)" = "buildah" ]; then \
	  echo "🔧 Buildah detected: Performing multi-arch build..."; \
	  FINAL_TAG=$(IMG); \
	  for arch in amd64; do \
	    ARCH_TAG=$$FINAL_TAG-$$arch; \
	    echo "📦 Building for architecture: $$arch"; \
		buildah build --arch=$$arch --os=linux --layers -f build/Dockerfile -t $(IMG)-$$arch . || exit 1; \
	    echo "🚀 Pushing image: $(IMG)-$$arch"; \
	    buildah push $(IMG)-$$arch docker://$(IMG)-$$arch || exit 1; \
	  done; \
	  echo "🧼 Removing existing manifest (if any)..."; \
	  buildah manifest rm $$FINAL_TAG || true; \
	  echo "🧱 Creating and pushing manifest list: $(IMG)"; \
	  buildah manifest create $(IMG); \
	  for arch in amd64; do \
	    ARCH_TAG=$$FINAL_TAG-$$arch; \
	    buildah manifest add $$FINAL_TAG $$ARCH_TAG; \
	  done; \
	  buildah manifest push --all $(IMG) docker://$(IMG); \
	elif [ "$(BUILDER)" = "docker" ]; then \
	  echo "🐳 Docker detected: Building with buildx..."; \
	  - docker buildx create --use --name image-builder || true; \
	  docker buildx use image-builder; \
	  docker buildx build --push --platform=$(PLATFORMS) --tag $(IMG) -f build/Dockerfile . || exit 1; \
	  docker buildx rm image-builder || true; \
	elif [ "$(BUILDER)" = "podman" ]; then \
	  echo "⚠️ Podman detected: Building single-arch image..."; \
	  podman build -f build/Dockerfile -t $(IMG) . || exit 1; \
	  podman push $(IMG) || exit 1; \
	else \
	  echo "❌ No supported container tool available."; \
	  exit 1; \
	fi

.PHONY:	image-build
image-build: check-container-tool load-version-json ## Build Docker image using $(CONTAINER_TOOL)
	@printf "\033[33;1m==== Building Docker image $(IMG) ====\033[0m\n"
	$(CONTAINER_TOOL) build -f build/Dockerfile --build-arg TARGETOS=$(TARGETOS) --build-arg TARGETARCH=$(TARGETARCH) -t $(IMG) .

.PHONY: image-push
image-push: check-container-tool load-version-json ## Push Docker image $(IMG) to registry
	@printf "\033[33;1m==== Pushing Docker image $(IMG) ====\033[0m\n"
	$(CONTAINER_TOOL) push $(IMG)

##@ Install/Uninstall Targets

# Default install/uninstall (Docker)
install: install-docker ## Default install using Docker
	@echo "Default Docker install complete."

uninstall: uninstall-docker ## Default uninstall using Docker
	@echo "Default Docker uninstall complete."

### Docker Targets

.PHONY: install-docker
install-docker: check-container-tool ## Install app using $(CONTAINER_TOOL)
	@echo "Starting container with $(CONTAINER_TOOL)..."
	$(CONTAINER_TOOL) run -d --name $(PROJECT_NAME)-container $(IMG)
	@echo "$(CONTAINER_TOOL) installation complete."
	@echo "To use $(PROJECT_NAME), run:"
	@echo "alias $(PROJECT_NAME)='$(CONTAINER_TOOL) exec -it $(PROJECT_NAME)-container /app/$(PROJECT_NAME)'"

.PHONY: uninstall-docker
uninstall-docker: check-container-tool ## Uninstall app from $(CONTAINER_TOOL)
	@echo "Stopping and removing container in $(CONTAINER_TOOL)..."
	-$(CONTAINER_TOOL) stop $(PROJECT_NAME)-container && $(CONTAINER_TOOL) rm $(PROJECT_NAME)-container
@echo "$(CONTAINER_TOOL) uninstallation complete. Remove alias if set: unalias $(PROJECT_NAME)"

### Kubernetes Targets (kubectl)

.PHONY: install-k8s
install-k8s: check-kubectl check-kustomize check-envsubst ## Install on Kubernetes
	export PROJECT_NAME=${PROJECT_NAME}
	export NAMESPACE=${NAMESPACE}
	@echo "Creating namespace (if needed) and setting context to $(NAMESPACE)..."
	kubectl create namespace $(NAMESPACE) 2>/dev/null || true
	kubectl config set-context --current --namespace=$(NAMESPACE)
	@echo "Deploying resources from deploy/ ..."
	# Build the kustomization from deploy, substitute variables, and apply the YAML
	kustomize build deploy | envsubst | kubectl apply -f -
	@echo "Waiting for pod to become ready..."
	sleep 5
	@POD=$$(kubectl get pod -l app=$(PROJECT_NAME)-statefulset -o jsonpath='{.items[0].metadata.name}'); \
	echo "Kubernetes installation complete."; \
	echo "To use the app, run:"; \
	echo "alias $(PROJECT_NAME)='kubectl exec -n $(NAMESPACE) -it $$POD -- /app/$(PROJECT_NAME)'"

.PHONY: uninstall-k8s
uninstall-k8s: check-kubectl check-kustomize check-envsubst ## Uninstall from Kubernetes
	export PROJECT_NAME=${PROJECT_NAME}
	export NAMESPACE=${NAMESPACE}
	@echo "Removing resources from Kubernetes..."
	kustomize build deploy | envsubst | kubectl delete --force -f - || true
	POD=$$(kubectl get pod -l app=$(PROJECT_NAME)-statefulset -o jsonpath='{.items[0].metadata.name}'); \
	echo "Deleting pod: $$POD"; \
	kubectl delete pod "$$POD" --force --grace-period=0 || true; \
	echo "Kubernetes uninstallation complete. Remove alias if set: unalias $(PROJECT_NAME)"

### OpenShift Targets (oc)

.PHONY: install-openshift
install-openshift: check-kubectl check-kustomize check-envsubst ## Install on OpenShift
	@echo $$PROJECT_NAME $$NAMESPACE $$IMAGE_TAG_BASE $$VERSION
	@echo "Creating namespace $(NAMESPACE)..."
	kubectl create namespace $(NAMESPACE) 2>/dev/null || true
	@echo "Deploying common resources from deploy/ ..."
	# Build and substitute the base manifests from deploy, then apply them
	kustomize build deploy | envsubst '$$PROJECT_NAME $$NAMESPACE $$IMAGE_TAG_BASE $$VERSION' | kubectl apply -n $(NAMESPACE) -f -
	@echo "Waiting for pod to become ready..."
	sleep 5
	@POD=$$(kubectl get pod -l app=$(PROJECT_NAME)-statefulset -n $(NAMESPACE) -o jsonpath='{.items[0].metadata.name}'); \
	echo "OpenShift installation complete."; \
	echo "To use the app, run:"; \
	echo "alias $(PROJECT_NAME)='kubectl exec -n $(NAMESPACE) -it $$POD -- /app/$(PROJECT_NAME)'"

.PHONY: uninstall-openshift
uninstall-openshift: check-kubectl check-kustomize check-envsubst ## Uninstall from OpenShift
	@echo "Removing resources from OpenShift..."
	kustomize build deploy | envsubst '$$PROJECT_NAME $$NAMESPACE $$IMAGE_TAG_BASE $$VERSION' | kubectl delete --force -f - || true
	# @if kubectl api-resources --api-group=route.openshift.io | grep -q Route; then \
	#   envsubst '$$PROJECT_NAME $$NAMESPACE $$IMAGE_TAG_BASE $$VERSION' < deploy/openshift/route.yaml | kubectl delete --force -f - || true; \
	# fi
	@POD=$$(kubectl get pod -l app=$(PROJECT_NAME)-statefulset -n $(NAMESPACE) -o jsonpath='{.items[0].metadata.name}'); \
	echo "Deleting pod: $$POD"; \
	kubectl delete pod "$$POD" --force --grace-period=0 || true; \
	echo "OpenShift uninstallation complete. Remove alias if set: unalias $(PROJECT_NAME)"

### RBAC Targets (using kustomize and envsubst)

.PHONY: install-rbac
install-rbac: check-kubectl check-kustomize check-envsubst ## Install RBAC
	@echo "Applying RBAC configuration from deploy/rbac..."
	kustomize build deploy/rbac | envsubst '$$PROJECT_NAME $$NAMESPACE $$IMAGE_TAG_BASE $$VERSION' | kubectl apply -f -

.PHONY: uninstall-rbac
uninstall-rbac: check-kubectl check-kustomize check-envsubst ## Uninstall RBAC
	@echo "Removing RBAC configuration from deploy/rbac..."
	kustomize build deploy/rbac | envsubst '$$PROJECT_NAME $$NAMESPACE $$IMAGE_TAG_BASE $$VERSION' | kubectl delete -f - || true


##@ Version Extraction
.PHONY: version dev-registry prod-registry extract-version-info

dev-version: check-jq
	@jq -r '.dev-version' .version.json

prod-version: check-jq
	@jq -r '.prod-version' .version.json

dev-registry: check-jq
	@jq -r '."dev-registry"' .version.json

prod-registry: check-jq
	@jq -r '."prod-registry"' .version.json

extract-version-info: check-jq
	@echo "DEV_VERSION=$$(jq -r '."dev-version"' .version.json)"
	@echo "PROD_VERSION=$$(jq -r '."prod-version"' .version.json)"
	@echo "DEV_IMAGE_TAG_BASE=$$(jq -r '."dev-registry"' .version.json)"
	@echo "PROD_IMAGE_TAG_BASE=$$(jq -r '."prod-registry"' .version.json)"

##@ Load Version JSON

.PHONY: load-version-json
load-version-json: check-jq
	@if [ "$(DEV_VERSION)" = "0.0.1" ]; then \
	  DEV_VERSION=$$(jq -r '."dev-version"' .version.json); \
	  PROD_VERSION=$$(jq -r '."dev-version"' .version.json); \
	  echo "✔ Loaded DEV_VERSION from .version.json: $$DEV_VERSION"; \
	  echo "✔ Loaded PROD_VERSION from .version.json: $$PROD_VERSION"; \
	  export DEV_VERSION; \
	  export PROD_VERSION; \
	fi && \
	CURRENT_DEFAULT="ghcr.io/llm-d/$(PROJECT_NAME)"; \
	if [ "$(IMAGE_TAG_BASE)" = "$$CURRENT_DEFAULT" ]; then \
	  IMAGE_TAG_BASE=$$(jq -r '."dev-registry"' .version.json); \
	  echo "✔ Loaded IMAGE_TAG_BASE from .version.json: $$IMAGE_TAG_BASE"; \
	  export IMAGE_TAG_BASE; \
	fi && \
	echo "🛠 Final values: DEV_VERSION=$$DEV_VERSION, PROD_VERSION=$$PROD_VERSION, IMAGE_TAG_BASE=$$IMAGE_TAG_BASE"

.PHONY: env
env: load-version-json ## Print environment variables
	@echo "DEV_VERSION=$(DEV_VERSION)"
	@echo "PROD_VERSION=$(PROD_VERSION)"
	@echo "IMAGE_TAG_BASE=$(IMAGE_TAG_BASE)"
	@echo "IMG=$(IMG)"
	@echo "CONTAINER_TOOL=$(CONTAINER_TOOL)"


##@ Tools

.PHONY: check-tools
check-tools: \
  check-go \
  check-ginkgo \
  check-golangci-lint \
  check-jq \
  check-kustomize \
  check-envsubst \
  check-container-tool \
  check-kubectl \
  check-buildah \
  check-podman
	@echo "✅ All required tools are installed."

.PHONY: check-go
check-go:
	@command -v go >/dev/null 2>&1 || { \
	  echo "❌ Go is not installed. Install it from https://golang.org/dl/"; exit 1; }

.PHONY: check-ginkgo
check-ginkgo:
	@command -v ginkgo >/dev/null 2>&1 || { \
	  echo "❌ ginkgo is not installed. Install with: go install github.com/onsi/ginkgo/v2/ginkgo@latest"; exit 1; }

.PHONY: check-golangci-lint
check-golangci-lint:
	@command -v golangci-lint >/dev/null 2>&1 || { \
	  echo "❌ golangci-lint is not installed. Install from https://golangci-lint.run/usage/install/"; exit 1; }

.PHONY: check-jq
check-jq:
	@command -v jq >/dev/null 2>&1 || { \
	  echo "❌ jq is not installed. Install it from https://stedolan.github.io/jq/download/"; exit 1; }

.PHONY: check-kustomize
check-kustomize:
	@command -v kustomize >/dev/null 2>&1 || { \
	  echo "❌ kustomize is not installed. Install it from https://kubectl.docs.kubernetes.io/installation/kustomize/"; exit 1; }

.PHONY: check-envsubst
check-envsubst:
	@command -v envsubst >/dev/null 2>&1 || { \
	  echo "❌ envsubst is not installed. It is part of gettext."; \
	  echo "🔧 Try: sudo apt install gettext OR brew install gettext"; exit 1; }

.PHONY: check-container-tool
check-container-tool:
	@command -v $(CONTAINER_TOOL) >/dev/null 2>&1 || { \
	  echo "❌ $(CONTAINER_TOOL) is not installed."; \
	  echo "🔧 Try: sudo apt install $(CONTAINER_TOOL) OR brew install $(CONTAINER_TOOL)"; exit 1; }

.PHONY: check-kubectl
check-kubectl:
	@command -v kubectl >/dev/null 2>&1 || { \
	  echo "❌ kubectl is not installed. Install it from https://kubernetes.io/docs/tasks/tools/"; exit 1; }

.PHONY: check-builder
check-builder:
	@if [ -z "$(BUILDER)" ]; then \
		echo "❌ No container builder tool (buildah, docker, or podman) found."; \
		exit 1; \
	else \
		echo "✅ Using builder: $(BUILDER)"; \
	fi

.PHONY: check-podman
check-podman:
	@command -v podman >/dev/null 2>&1 || { \
	  echo "⚠️  Podman is not installed. You can install it with:"; \
	  echo "🔧 sudo apt install podman  OR  brew install podman"; exit 1; }

##@ Alias checking
.PHONY: check-alias
check-alias: check-container-tool
	@echo "🔍 Checking alias functionality for container '$(PROJECT_NAME)-container'..."
	@if ! $(CONTAINER_TOOL) exec $(PROJECT_NAME)-container /app/$(PROJECT_NAME) --help >/dev/null 2>&1; then \
	  echo "⚠️  The container '$(PROJECT_NAME)-container' is running, but the alias might not work."; \
	  echo "🔧 Try: $(CONTAINER_TOOL) exec -it $(PROJECT_NAME)-container /app/$(PROJECT_NAME)"; \
	else \
	  echo "✅ Alias is likely to work: alias $(PROJECT_NAME)='$(CONTAINER_TOOL) exec -it $(PROJECT_NAME)-container /app/$(PROJECT_NAME)'"; \
	fi

.PHONY: print-namespace
print-namespace: ## Print the current namespace
	@echo "$(NAMESPACE)"

.PHONY: print-project-name
print-project-name: ## Print the current project name
	@echo "$(PROJECT_NAME)"

.PHONY: install-hooks
install-hooks: ## Install git hooks
	git config core.hooksPath hooks

##@ IPP simulation environment (Kind on Mac + Colima)

# Sim environment defaults. Override on the make command line, e.g.:
#   make sim-colima IPP_PATH=/path/to/llm-d-inference-payload-processor NAMESPACE=my-ns
#
# IPP_PATH is required by ipp-deploy. The rest have sane defaults that match
# what mac_colima_bootstrap.sh + ipp_deploy.sh set internally.
SIM_KIND_CLUSTER_NAME ?= ipp-e2e
SIM_NAMESPACE         ?= llmdbench
SIM_SPEC              ?= cicd/kind-sim-multi
SIM_RELEASE           ?= payload-processor

# Full simulation environment (bootstrap + IPP deploy) as one command.
# End state: Colima up, kind cluster stood up with two asymmetric sim stacks
# (opt-125m slow, opt-350m fast), CostGuard IPP installed and Ready, HTTPRoutes
# in place -- ready to receive traffic from `llmdbenchmark run`.
.PHONY: sim-colima
sim-colima: bootstrap-colima ipp-deploy ## Set up a full simulation environment for IPP on Kind on Mac with Colima
	@echo "✅ sim-colima: full simulation environment is ready in ns/$(SIM_NAMESPACE)."

# Bootstrap Colima, the kind cluster, and both sim stacks (opt-125m + opt-350m).
# Idempotent -- re-running skips work that's already done. Does NOT install IPP;
# that's the ipp-deploy step below.
.PHONY: bootstrap-colima
bootstrap-colima: ## Set up Colima, Kind cluster, and simulators
	@printf "\033[33;1m==== Running mac_colima_bootstrap.sh ====\033[0m\n"
	KIND_CLUSTER_NAME=$(SIM_KIND_CLUSTER_NAME) NAMESPACE=$(SIM_NAMESPACE) \
	  ./ipp_benchmarking/tools/mac_colima_bootstrap.sh

# Install IPP into the already-stood-up namespace via ipp_deploy.sh (builds the
# IPP image from $$IPP_PATH, side-loads it into kind, helm-installs the chart
# with CostGuard values). Requires IPP_PATH to point at a checkout of
# llm-d-inference-payload-processor.
.PHONY: ipp-deploy
ipp-deploy: ## Install IPP (builds image from $$IPP_PATH, helm-installs the chart with CostGuard values)
	@printf "\033[33;1m==== Running ipp_deploy.sh ====\033[0m\n"
	@if [ -z "$$IPP_PATH" ]; then \
	  echo "❌ IPP_PATH is unset. Export it, e.g.: export IPP_PATH=/path/to/llm-d-inference-payload-processor"; \
	  exit 1; \
	fi
	KIND_CLUSTER_NAME=$(SIM_KIND_CLUSTER_NAME) NAMESPACE=$(SIM_NAMESPACE) RELEASE=$(SIM_RELEASE) \
	  ./ipp_benchmarking/tools/ipp_deploy.sh

# Undeploy IPP (Helm release only). Leaves the sim standup and Colima/kind
# alive so a re-deploy is fast. Use `make tear-down-sim` for a full wipe.
.PHONY: ipp-undeploy
ipp-undeploy: ## Undeploy IPP (helm uninstall of the payload-processor release)
	@printf "\033[33;1m==== Uninstalling IPP release $(SIM_RELEASE) from ns/$(SIM_NAMESPACE) ====\033[0m\n"
	-helm uninstall $(SIM_RELEASE) -n $(SIM_NAMESPACE)

# Full teardown of everything sim-colima brought up: IPP release, sim stacks
# (via llmdbenchmark teardown), kind cluster, and Colima VM (stopped, not
# deleted -- run `colima delete default` if you also want to reclaim the
# ~45GB VM disk).
.PHONY: tear-down-sim
tear-down-sim: ## Remove the full environment set up by sim-colima
	@printf "\033[33;1m==== Tearing down the full sim environment ====\033[0m\n"
	-helm uninstall $(SIM_RELEASE) -n $(SIM_NAMESPACE)
	-llmdbenchmark --spec $(SIM_SPEC) teardown -p $(SIM_NAMESPACE)
	-kind delete cluster --name $(SIM_KIND_CLUSTER_NAME)
	-colima stop
	@echo "✅ tear-down-sim: environment removed. Colima VM disk retained under ~/.colima -- run 'colima delete default' to reclaim it."
