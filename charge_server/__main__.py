#!/usr/bin/env python3

import connexion

from charge_server import encoder
from charge_server import charge_server


def main():
    charge_server.init()

    app = connexion.App(__name__, specification_dir='./swagger/')
    app.app.json_encoder = encoder.JSONEncoder
    app.add_api('swagger.yaml', arguments={
            'title': 'Charge Assign', 'swagger_ui': False})
    app.run(port=8080)


if __name__ == '__main__':
    main()
