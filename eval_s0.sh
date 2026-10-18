#!/bin/bash
set -e
python src/predict.py tt-challenge/scenarios/S0.json -o /tmp/s0_eval.json 2>&1 | grep "^S0:"
python tt-challenge/platform/evaluate_golden.py tt-challenge/gold/S0.json /tmp/s0_eval.json 2>&1
