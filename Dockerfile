FROM python:3.7.16


WORKDIR /Users/jocotton/Desktop/tmp/SpliceAI-lookup


ADD . .

RUN pip install --upgrade pip
RUN pip install "setuptools<58" --upgrade

RUN pip install -r requiretments_for_Docker.txt

# Delete .decode('utf-8')
RUN sed -i  "242s/.decode('utf-8')//" /usr/local/lib/python3.7/site-packages/keras/models.py
# Delete 5th line to make tensoflow to work with other Python libraries 
RUN sed -i -e '5d' /usr/local/lib/python3.7/site-packages/keras/backend/tensorflow_backend.py

# Make tensorflow compatible downgrade the behaviour of v2
RUN sed -i "5i import tensorflow.compat.v1 as tf" /usr/local/lib/python3.7/site-packages/keras/backend/tensorflow_backend.py
RUN sed  -i "6i tf.disable_v2_behavior() " /usr/local/lib/python3.7/site-packages/keras/backend/tensorflow_backend.py

# Delete .decode('utf-8')
RUN sed -i  "s/.decode('utf8')//g" /usr/local/lib/python3.7/site-packages/keras/engine/topology.py

# To run in the server
RUN sed -i  's/127.0.0.1/0.0.0.0/g' start_local_server.sh

# Add a title

RUN sed -i '97i  <div style="font-family: Lucida Console; display:inline-block; min-width: 500px; font-size: 40px; color: blue; margin-bottom: 5px;">Spliceai lookup</div>' index.html

RUN sed -i '98i  <div style="display:inline-block; min-width: 430px; margin-top: 10px;">A local version engineered by the Bioinformatics Team, exclusively tailored for the use of Wessex Genomics Laboratory Services.</div>' index.html

#Run sed -i '99i  <div style="display:inline-block; min-width: 430px; margin-top: 10px;color: red;">Not yet approved for clinical analysis.</div>' index.html



EXPOSE 8080

CMD ./start_local_server.sh

