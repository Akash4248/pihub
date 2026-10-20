# PIHUB Distributed Deployment Guide

This guide explains how to deploy PIHUB across a two-node architecture: a powerful Main Server for AI/Heavy lifting, and a Raspberry Pi for lightweight API routing and static file serving.

## Architecture Overview

The system uses two `docker-compose` files:
1.  **Main Server (`docker-compose.server.yml`)**: Runs `qdrant` (Vector DB), `content-pipeline` (Embeddings), `inference-service` (LLMs).
2.  **Raspberry Pi (`docker-compose.pi.yml`)**: Runs `pihub` (Node backend), `pack-service` (Textbook Serving), `experiment-service` (PhET Sims), `gateway` (API Router), and `nginx`.

## Prerequisites

*   **Server Hostname**: Make sure your main server is reachable via mDNS at `akash-ubuntu.local`.
*   **Raspberry Pi Hostname**: Should be reachable on the same network (e.g., `pi.local`).
*   **NFS (Network File System)**: The Server must share its storage folder so the Pi can read the textbooks, models, and generated packs in real-time.

---

## Step 1: Set Up NFS on the Main Server

The Raspberry Pi needs access to the server's files without copying them physically.

1.  **Install NFS Kernel Server** on your Ubuntu Server:
    ```bash
    sudo apt update
    sudo apt install nfs-kernel-server
    ```

2.  **Export the PIHUB Directories**:
    Open `/etc/exports` in a text editor (e.g., `sudo nano /etc/exports`) and add the following lines to share the PIHUB directories with the local network (replace `192.168.1.0/24` with your actual subnet if needed, or use `*` for testing on a trusted LAN):
    ```text
    /home/akash/Desktop/PIHUB/backend/volumes/shared_storage *(rw,sync,no_subtree_check,no_root_squash)
    /home/akash/Desktop/PIHUB/generated_pack *(ro,sync,no_subtree_check,no_root_squash)
    /home/akash/Desktop/NOTIMP/TEXTBOOKS *(ro,sync,no_subtree_check,no_root_squash)
    /home/akash/Desktop/PIHUB/phet_downloads *(ro,sync,no_subtree_check,no_root_squash)
    ```

3.  **Apply the Exports**:
    ```bash
    sudo exportfs -a
    sudo systemctl restart nfs-kernel-server
    ```

---

## Step 2: Configure the Raspberry Pi Environment

The Raspberry Pi needs to know where to send heavy requests.

1.  Open the `.env` file located in the `backend/` directory on the Raspberry Pi.
2.  Update the Service URLs to point to `akash-ubuntu.local` instead of the internal Docker hostnames:
    ```env
    QDRANT_URL=http://akash-ubuntu.local:6333
    CONTENT_PIPELINE_URL=http://akash-ubuntu.local:8001
    INFERENCE_SERVICE_URL=http://akash-ubuntu.local:8010
    ```

---

## Step 3: Launching the Containers

### 1. Start the Main Server
On `akash-ubuntu.local`, navigate to the `backend/` directory and launch the server-side containers:
```bash
cd /home/akash/Desktop/PIHUB/backend
docker compose -f docker-compose.server.yml up -d
```
*Wait a minute for the models and vector databases to fully initialize.*

### 2. Start the Raspberry Pi
On the Raspberry Pi, navigate to the `backend/` directory and launch the lightweight containers:
```bash
cd /path/to/PIHUB/backend
docker compose -f docker-compose.pi.yml up -d
```

Docker on the Pi will automatically use the `nfs` driver to mount the folders from `akash-ubuntu.local`.

---

## Troubleshooting

*   **NFS Mount Fails on Pi**: Ensure the Pi has `nfs-common` installed (`sudo apt install nfs-common`).
*   **Host Not Found**: If `akash-ubuntu.local` cannot be resolved, ensure `avahi-daemon` is running on both machines. Alternatively, replace `akash-ubuntu.local` in `.env` and `docker-compose.pi.yml` with the actual static IP address of the server.
*   **Permissions Issues**: Check the permissions of the exported directories on the server to ensure the Docker daemon on the Pi can read/write to them.
