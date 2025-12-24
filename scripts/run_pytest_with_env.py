import os
import sys
import subprocess

# Set Atlas URI here (from .env)
os.environ['MONGODB_URL'] = 'mongodb+srv://Eshwar:Eshwar333@cluster0.6bl1tzp.mongodb.net/'
print('MONGODB_URL=', os.environ['MONGODB_URL'])

# Run pytest with the same interpreter
rc = subprocess.call([sys.executable, '-m', 'pytest', '-q'])
sys.exit(rc)
