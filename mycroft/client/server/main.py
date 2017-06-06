# Copyright 2016 Mycroft AI, Inc.
#
# This file is part of Mycroft Core.
#
# Mycroft Core is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Mycroft Core is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Mycroft Core.  If not, see <http://www.gnu.org/licenses/>.

import socket, select, sys, time, json
from threading import Thread

from mycroft.messagebus.client.ws import WebsocketClient
from mycroft.messagebus.message import Message
from mycroft.util.log import getLogger


ws = None
logger = getLogger("Mycroft_Server")

# List to keep track of socket descriptors
CONNECTION_LIST = []
RECV_BUFFER = 4096  # Advisable to keep it as an exponent of 2
PORT = 5000
server_socket = None
utterance_socket = None

blacklisted_ips = []
allowed_bus_messages = ["recognizer_loop:utterance", "names_response", "id_update"]
names = {}

chatting = False
waiting = False
more = False
response = ""
default_answer = "i don't know how to answer that"
metadata = {}


def handle_failure(event):
    global waiting, response, default_answer
    response = default_answer
    logger.debug("intent failure detected")


def handle_speak(event):
    global chatting, waiting, more, response, metadata
    utterance = event.data.get('utterance')
    target = event.data.get('target')
    data = event.data.get('metadata')
    if data is None:
        metadata = {}
    else:
        metadata = data
    logger.debug("Speak: " + utterance + " Target: " + target)
    # if we are chatting and waiting for a response
    # TODO process target
    if chatting and waiting:
        # capture response
        logger.debug("Capturing speech response")
        response = utterance
        more = event.data.get("more")
        waiting = False


def wait_answer():
    global waiting
    start = time.time()
    elapsed = 0
    logger.debug( "Waiting for speech response")
    waiting = True
    # wait maximum 20 seconds
    while waiting and elapsed < 20:
        elapsed = time.time() - start
        time.sleep(0.1)
    waiting = False


def connect():
    ws.run_forever()


# Function to broadcast messages to all connected clients
def broadcast_data(sock, message):
    # Do not send the message to master socket and the client who has send us the message
    for socket in CONNECTION_LIST:
        if socket != server_socket and socket != sock:
            try:
                logger.debug("Broadcasting " + message)
                socket.send(message)
            except:
                # broken socket connection may be, chat client pressed ctrl+c for example
                # offline_client(sock, addr)
                pass


# answer
def answer_data(sock, message):
    # send the message to the client who has send us the message
    for socket in CONNECTION_LIST:
        if socket == sock:
            try:
                socket.send(message)
            except:
                # broken socket connection may be, chat client pressed ctrl+c for example
                offline_client(sock)


def offline_client(sock):
    global names
    try:
        sock.close()
        CONNECTION_LIST.remove(sock)
        # broadcast_data(sock, "Client (%s, %s) is offline" % addr)
        ip, user = str(sock.getpeername()).replace("(", "").replace(")", "").replace(" ", "").split(",")
        logger.debug("Client is offline: " + str(sock.getpeername()))
        names.pop(int(user), None)
    except:
        # already removed
        pass


# answer id
def answer_id(sock):
    # send the message to the client who has send us the message
    for socket in CONNECTION_LIST:
        if socket == sock:
            try:
                ip, user = sock.getpeername()
                logger.debug("Sending Id to Client " + str(sock.getpeername()))
                answer = get_msg(Message("id", {"id": user}))
                socket.send(answer)
            except:
                # broken socket connection may be, chat client pressed ctrl+c for example
                offline_client(sock)


def get_answer(utterance, user):
    global more, chatting, ws, response
    logger.debug("emitting utterance to bus: " + utterance)
    ws.emit(
       Message("recognizer_loop:utterance",
               {'utterances': [utterance.strip()], 'source': str(user), "user": "unknown", "mute": True}))

    logger.debug("Waiting answer for user " + user)
    # capture speech response
    wait_answer()
    # if more speech is coming for this chat
    answer = response
    while more:
        logger.debug( "More speech is expected, waiting")
        # capture speech response
        wait_answer()
        if response not in answer:
            # if wait ended not because of time_out, append answer
            answer += "\n" + response
    data = {"utterance": answer, 'target': user, "mute": False, "more": False, "expect_response": False, "metadata":metadata}
    answer_type = "speak"
    return answer_type, data


def get_msg(message):
    if hasattr(message, 'serialize'):
        return message.serialize()
    else:
        return json.dumps(message.__dict__)


def send_message(sock, type="speak", data={}):
    message = get_msg(Message(type, data))
    answer_data(sock, message)


def handle_message_request(event):
    user_id = event.data.get("user")
    type = event.data.get("type")
    data = event.data.get("data")
    for socket in CONNECTION_LIST:
        ip, user = socket.getppername().replace("(", "").replace(")", "").replace(" ", "").split(",")
        if user_id == user:
            send_message(socket, type, data)

def main():
    global ws
    ws = WebsocketClient()
    ws.on('speak', handle_speak)
    ws.on('intent_failure', handle_failure)
    ws.on('message_request', handle_message_request)
    event_thread = Thread(target=connect)
    event_thread.setDaemon(True)
    event_thread.start()

    global CONNECTION_LIST, RECV_BUFFER, PORT, server_socket, more, chatting, response, names, metadata
    # start server socket
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("0.0.0.0", PORT))
    server_socket.listen(10)

    # Add server socket to the list of readable connections
    CONNECTION_LIST.append(server_socket)

    logger.debug("Listening started on port " + str(PORT))

    while True:
        # Get the list sockets which are ready to be read through select
        read_sockets, write_sockets, error_sockets = select.select(CONNECTION_LIST, [], [])

        for sock in read_sockets:
            # New connection
            if sock == server_socket:
                # Handle the case in which there is a new connection received through server_socket
                sockfd, addr = server_socket.accept()
                CONNECTION_LIST.append(sockfd)
                logger.debug( "Client (%s, %s) connected" % addr )
                ip, user = str(addr).replace("(", "").replace(")", "").replace(" ", "").split(",")
                # see if blacklisted
                if ip not in blacklisted_ips:
                    # tell other clients this is available
                    #broadcast_data(sockfd, "[%s:%s] is available\n" % addr, addr)
                    # tell client it's id
                    answer_id(sockfd)
                else:
                #  if blacklisted kick
                    offline_client(sockfd)
            # Some incoming message from a client
            else:
                # Data received from client, process it
                try:
                    utterance = sock.recv(RECV_BUFFER)
                    if utterance:
                        ip, user = str(sock.getpeername()).replace("(", "").replace(")", "").replace(" ", "").split(",")
                        logger.debug("received: " + str(utterance).strip() + " from socket: " + user + " from ip: " + ip)
                        deserialized_message = Message.deserialize(utterance)
                        if deserialized_message.type in allowed_bus_messages:
                            # TODO make queue per user, so other dont wait for "long skills"
                            data = deserialized_message.data

                            if data.get("id") is None:
                                data["id"] = user
                            elif data["id"] == "unknown":
                                data["id"] = user

                            if deserialized_message.type == "names_response":
                                for name in data["names"]:
                                    logger.debug("Setting alias: " + name + " for socket: " + str(data["id"]))
                                    names[name] = data["id"]
                            elif deserialized_message.type == "id_update":
                                answer_id(sock)
                            elif deserialized_message.type == "recognizer_loop:utterance":
                                utterance = data["utterances"][0]
                                # get answer
                                more = False
                                metadata = {}
                                chatting = True
                                # answer
                                answer_type, answer_data = get_answer(utterance, user)
                                logger.debug("answering: " + str(answer_data) + " to user: " + user)
                                send_message(sock, answer_type, answer_data)
                                if "dream_url" in metadata.keys():
                                    logger.info("sending formatted dream result")
                                    send_message(sock, "deep_dream_result", {"dream_url": metadata["dream_url"]})
                                chatting = False
                except:
                    offline_client(sock)
                    continue
    server_socket.close()


if __name__ == "__main__":
    main()
