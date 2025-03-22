import os
os.environ['OTEL_SDK_DISABLED'] = "True"
import sys
# from crewai.cli.cli import crewai
from template_test.main import run,train,replay,test
run()
pass
