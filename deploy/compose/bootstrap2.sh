#!/bin/bash
export PUSH_NODE_TYPE=bootstrap
docker-compose -p "push" -f push-node-bootstrap-2.yml $*
