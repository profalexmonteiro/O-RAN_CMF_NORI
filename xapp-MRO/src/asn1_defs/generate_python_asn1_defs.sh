#!/bin/bash

# Using the Pycrate compiler (installed with pip)
pycrate_compiler="/home/openran-br/.local/bin/pycrate_asn1compile.py"
asn1_files="e2ap-v02.02.03.asn1"
output_file="e2ap_2_3"
python3 $pycrate_compiler -i $asn1_files -o $output_file

pycrate_compiler="/home/openran-br/.local/bin/pycrate_asn1compile.py"
asn1_files="e2sm-kpm-rc.asn"
output_file="e2sm_kpm_rc"
python3 $pycrate_compiler -i $asn1_files -o $output_file