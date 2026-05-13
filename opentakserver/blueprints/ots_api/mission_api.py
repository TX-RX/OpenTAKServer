import datetime
import json
import traceback
import uuid
from xml.etree.ElementTree import tostring

import bleach
import pika
import sqlalchemy.exc
from flask import Blueprint
from flask import current_app as app
from flask import jsonify, request
from flask_babel import gettext
from flask_security import (
    auth_required,
    current_user,
    hash_password,
    roles_required,
    verify_password,
)
from sqlalchemy import or_

from opentakserver.blueprints.marti_api.mission_marti_api import (
    generate_invitation_cot,
    generate_mission_delete_cot,
    generate_new_mission_cot,
    invite,
)
from opentakserver.blueprints.ots_api.api import paginate, search
from opentakserver.extensions import db, logger
from opentakserver.models.Chatrooms import Chatroom
from opentakserver.models.ChatroomsUids import ChatroomsUids
from opentakserver.models.CoT import CoT
from opentakserver.models.EUD import EUD
from opentakserver.models.GeoChat import GeoChat
from opentakserver.models.Group import Group
from opentakserver.models.GroupMission import GroupMission
from opentakserver.models.GroupUser import GroupUser
from opentakserver.models.Mission import Mission
from opentakserver.models.MissionChange import MissionChange, generate_mission_change_cot
from opentakserver.models.MissionContent import MissionContent
from opentakserver.models.MissionContentMission import MissionContentMission
from opentakserver.models.MissionInvitation import MissionInvitation
from opentakserver.models.MissionLogEntry import MissionLogEntry
from opentakserver.models.MissionRole import MissionRole
from opentakserver.models.MissionUID import MissionUID

data_sync_api = Blueprint("data_sync_api", __name__)


@data_sync_api.route("/api/missions")
@auth_required()
def get_missions():
    query: db.Query = db.session.query(Mission)
    query = search(query, Mission, "name")
    query = search(query, Mission, "guid")
    query = search(query, Mission, "tool")

    # Only show users missions that belong to the same groups they belong to
    if not current_user.has_role("administrator"):
        group_filters = []
        groups = db.session.execute(
            db.session.query(GroupUser).filter_by(user_id=current_user.id, direction=Group.IN)
        ).scalars()
        for group in groups:
            group_filters.append(GroupMission.group_id == group.group_id)
        if group_filters:
            query = query.outerjoin(GroupMission).where(or_(*group_filters)).distinct(Mission.name)

    return paginate(query, Mission)


@data_sync_api.route("/api/missions", methods=["PUT", "POST"])
@auth_required()
def create_edit_mission():
    mission_name = request.json.get("name")
    creator_uid = request.json.get("creator_uid")
    if not mission_name or not creator_uid:
        return (
            jsonify({"success": False, "error": "Please provide a mission name and creator UID"}),
            400,
        )

    mission_name = bleach.clean(mission_name)
    creator_uid = bleach.clean(creator_uid)

    mission = db.session.execute(db.session.query(Mission).filter_by(name=mission_name)).first()

    # Creates a new mission
    if not mission:
        eud = db.session.execute(db.session.query(EUD).filter_by(uid=creator_uid)).first()
        if not eud:
            return jsonify({"success": False, "error": f"Invalid UID: {creator_uid}"}), 400

        groups = None
        mission_groups = []

        mission = Mission()
        mission.create_time = datetime.datetime.now(datetime.timezone.utc)
        mission.guid = str(uuid.uuid4())
        mission.creator_uid = creator_uid

        for key in request.json.keys():
            if key == "password" and request.json.get("password"):
                mission.password = hash_password(request.json.get("password"))
            elif key == "groups":
                group_ids = request.json.get("groups")
                groups = db.session.execute(
                    db.session.query(GroupUser).filter_by(
                        user_id=current_user.id, enabled=True, direction=Group.IN
                    )
                ).scalars()

                # Make sure the user is a member of the IN group that they want the mission to be associated with. Also allow
                # administrators to associate a mission with any group
                for group_id in group_ids:
                    user_in_group = False

                    if not current_user.has_role("administrator"):
                        for group in groups:
                            if group.group_id == int(group_id):
                                user_in_group = True
                                break

                    if not user_in_group and not current_user.has_role("administrator"):
                        group = db.session.execute(
                            db.session.query(Group).filter_by(id=group_id)
                        ).first()
                        if group:
                            return (
                                jsonify(
                                    {
                                        "success": False,
                                        "error": f"User is not a member of {group[0].name}",
                                    }
                                ),
                                403,
                            )
                        else:
                            return (
                                jsonify(
                                    {"success": False, "error": f"Invalid group ID: {group_id}"}
                                ),
                                400,
                            )

                    group_id = int(group_id)
                    group = db.session.execute(
                        db.session.query(Group).filter_by(id=group_id)
                    ).first()
                    if not group:
                        continue

                    group_mission = GroupMission()
                    group_mission.mission_name = mission_name
                    group_mission.group_id = group_id
                    mission_groups.append(group_mission)

            elif hasattr(mission, key):
                setattr(mission, key, request.json[key])
            else:
                return (
                    jsonify(
                        {"success": False, "error": gettext("Invalid property: %(key)s", key=key)}
                    ),
                    400,
                )

        mission.password_protected = mission.password != "" and mission.password is not None

        role = MissionRole()
        role.clientUid = creator_uid
        role.username = current_user.username
        role.createTime = datetime.datetime.now(datetime.timezone.utc)
        role.role_type = MissionRole.MISSION_OWNER
        role.mission_name = mission_name

        invitation = MissionInvitation()
        invitation.mission_name = mission_name
        invitation.client_uid = creator_uid
        invitation.creator_uid = creator_uid
        invitation.role = MissionRole.MISSION_OWNER

        try:
            db.session.add(mission)
            db.session.add(role)
            db.session.add(invitation)
            db.session.commit()

            for group in mission_groups:
                db.session.add(group)
            db.session.commit()
        except sqlalchemy.exc.IntegrityError as e:
            logger.error(f"Failed to add mission: {e}")
            logger.debug(mission.serialize())
            return (
                jsonify(
                    {"success": False, "error": gettext("Failed to add mission: %(e)s", e=str(e))}
                ),
                400,
            )

        rabbit_credentials = pika.PlainCredentials(
            app.config.get("OTS_RABBITMQ_USERNAME"), app.config.get("OTS_RABBITMQ_PASSWORD")
        )
        rabbit_host = app.config.get("OTS_RABBITMQ_SERVER_ADDRESS")
        rabbit_connection = pika.BlockingConnection(
            pika.ConnectionParameters(host=rabbit_host, credentials=rabbit_credentials)
        )
        channel = rabbit_connection.channel()

        for group in groups or []:
            logger.error(f"Publishing to {group.group.name}.{group.direction}")
            channel.basic_publish(
                exchange="groups",
                routing_key=f"{group.group.name}.{group.direction}",
                body=json.dumps(
                    {
                        "uid": app.config.get("OTS_NODE_ID"),
                        "cot": tostring(generate_new_mission_cot(mission)).decode("utf-8"),
                    }
                ),
            )

        channel.basic_publish(
            exchange="dms",
            routing_key=creator_uid,
            body=json.dumps(
                {
                    "uid": creator_uid,
                    "cot": tostring(generate_invitation_cot(mission, creator_uid)).decode("utf-8"),
                }
            ),
        )
        channel.close()

        return jsonify({"success": True})

    # Update an existing mission

    # Checks if current user is the mission creator
    mission = mission[0]
    is_user_mission_creator = False
    for eud in current_user.euds:
        if eud.uid == mission.creator_uid:
            is_user_mission_creator = True
            break

    # Only allows admins and the mission creator to change existing missions
    if not current_user.has_role("administrator") and not is_user_mission_creator:
        return (
            jsonify(
                {
                    "success": False,
                    "error": gettext(
                        "Only an admin or the mission creator can change this mission"
                    ),
                }
            ),
            403,
        )

    for key in request.json:
        if key == "password" and request.json.get("password"):
            mission.password = hash_password(request.json.get("password"))
        elif key == "groups" and request.json.get("groups"):
            db.session.execute(sqlalchemy.delete(GroupMission).filter_by(mission_name=mission_name))
            db.session.commit()

            for group_id in request.json.get("groups"):
                group_id = int(group_id)
                group = db.session.execute(db.session.query(Group).filter_by(id=group_id)).first()
                if not group:
                    continue

                group_mission = GroupMission()
                group_mission.mission_name = mission_name
                group_mission.group_id = group_id
                db.session.add(group_mission)
            db.session.commit()
        elif hasattr(mission, key):
            setattr(mission, key, request.json.get(key))
        else:
            return jsonify({"success": False, "error": gettext("Invalid property: %(key)s")}), 400

    db.session.execute(
        sqlalchemy.update(Mission)
        .filter(Mission.name == mission_name)
        .values(**mission.serialize())
    )
    db.session.commit()

    return jsonify({"success": True})


@data_sync_api.route("/api/missions", methods=["DELETE"])
@roles_required("administrator")
def delete_mission():
    mission_name = request.args.get("name")
    if not mission_name:
        return jsonify({"success": False, "error": gettext("Please specify a mission name")}), 404

    mission_name = bleach.clean(mission_name)

    mission = db.session.execute(db.session.query(Mission).filter_by(name=mission_name)).first()
    if not mission:
        return (
            jsonify(
                {
                    "success": False,
                    "error": gettext(
                        "Mission %(mission_name)s not found", mission_name=mission_name
                    ),
                }
            ),
            404,
        )
    mission = mission[0]

    db.session.execute(
        sqlalchemy.delete(GroupMission).where(GroupMission.mission_name == mission_name)
    )
    db.session.execute(
        sqlalchemy.delete(MissionContentMission).where(
            MissionContentMission.mission_name == mission_name
        )
    )
    db.session.execute(
        sqlalchemy.delete(MissionChange).where(MissionChange.mission_name == mission_name)
    )

    chatroom: Chatroom | None = db.session.execute(
        db.session.query(Chatroom).where(Chatroom.name == mission_name)
    ).scalar()
    if chatroom:
        db.session.execute(
            sqlalchemy.delete(ChatroomsUids).where(ChatroomsUids.chatroom_id == chatroom.id)
        )
        db.session.execute(sqlalchemy.delete(GeoChat).where(GeoChat.chatroom_id == chatroom.id))
        db.session.delete(chatroom)

    db.session.execute(sqlalchemy.delete(CoT).where(CoT.mission_name == mission_name))
    db.session.execute(
        sqlalchemy.delete(MissionInvitation).where(MissionInvitation.mission_name == mission_name)
    )
    db.session.execute(
        sqlalchemy.delete(MissionLogEntry).where(MissionLogEntry.mission_name == mission_name)
    )
    db.session.execute(
        sqlalchemy.delete(MissionRole).where(MissionRole.mission_name == mission_name)
    )
    db.session.execute(sqlalchemy.delete(MissionUID).where(MissionUID.mission_name == mission_name))
    db.session.execute(sqlalchemy.delete(Mission).where(Mission.name == mission_name))
    db.session.commit()

    rabbit_credentials = pika.PlainCredentials(
        app.config.get("OTS_RABBITMQ_USERNAME"), app.config.get("OTS_RABBITMQ_PASSWORD")
    )
    rabbit_host = app.config.get("OTS_RABBITMQ_SERVER_ADDRESS")
    rabbit_connection = pika.BlockingConnection(
        pika.ConnectionParameters(host=rabbit_host, credentials=rabbit_credentials)
    )
    channel = rabbit_connection.channel()
    channel.basic_publish(
        exchange="missions",
        routing_key="missions",
        body=json.dumps(
            {
                "uid": app.config.get("OTS_NODE_ID"),
                "cot": tostring(generate_mission_delete_cot(mission)).decode("utf-8"),
            }
        ),
    )
    channel.close()
    rabbit_connection.close()

    return jsonify({"success": True})


@data_sync_api.route("/api/missions/invite", methods=["POST"])
@auth_required()
def invite_eud():
    mission_name = request.json["mission_name"]
    eud_uid = request.json["uid"]
    mission = db.session.execute(db.session.query(Mission).filter_by(name=mission_name)).first()
    if not mission:
        return (
            jsonify(
                {
                    "success": False,
                    "error": gettext(
                        "Mission not found: %(mission_name)s", mission_name=mission_name
                    ),
                }
            ),
            404,
        )
    mission = mission[0]

    # If the user isn't an admin an the mission is password protected, verify the password
    if (
        mission.password_protected
        and not current_user.has_role("administrator")
        and not request.json.get("password")
    ):
        return (
            jsonify({"success": False, "error": gettext("Please provide the mission password")}),
            403,
        )

    elif (
        mission.password_protected
        and not current_user.has_role("administrator")
        and not verify_password(request.json.get("password"), mission.password)
    ):
        return jsonify({"success": False, "error": gettext("Invalid password")}), 401

    return invite(mission_name, "clientuid", eud_uid)


@data_sync_api.route(
    "/api/missions/guid/<mission_guid>/attach_content", methods=["POST"]
)
@roles_required("administrator")
def attach_mission_content(mission_guid: str):
    """Attach already-uploaded MissionContent rows to a mission identified by GUID.

    Recovery path for orphan content created when a mission is recreated under
    the same name (issue #300: Mission.name is the PK and FKs do not cascade).
    Keying by GUID lets this survive future renames of the target mission.
    """
    body = request.get_json(silent=True) or {}
    hashes = body.get("hashes")
    if not isinstance(hashes, list) or not all(isinstance(h, str) for h in hashes):
        return (
            jsonify(
                {"success": False, "error": gettext("Body must contain a 'hashes' list of strings")}
            ),
            400,
        )

    mission = db.session.execute(
        db.session.query(Mission).filter_by(guid=mission_guid)
    ).first()
    if not mission:
        return (
            jsonify(
                {
                    "success": False,
                    "error": gettext(
                        "No such mission with guid %(mission_guid)s", mission_guid=mission_guid
                    ),
                }
            ),
            404,
        )
    mission = mission[0]
    mission_name = mission.name

    # First pass: resolve every hash before we mutate anything.
    contents: list[MissionContent] = []
    missing: list[str] = []
    for content_hash in hashes:
        row = db.session.execute(
            db.session.query(MissionContent).filter_by(hash=content_hash)
        ).first()
        if row is None:
            missing.append(content_hash)
        else:
            contents.append(row[0])

    if missing:
        return (
            jsonify(
                {
                    "success": False,
                    "error": gettext("Unknown content hash(es)"),
                    "missing": missing,
                }
            ),
            404,
        )

    # Second pass: insert link rows + change rows in a single transaction.
    newly_attached: list[dict] = []
    already_attached: list[dict] = []
    new_changes: list[tuple[MissionChange, MissionContent]] = []

    for content in contents:
        existing_link = db.session.execute(
            db.session.query(MissionContentMission).filter_by(
                mission_content_id=content.id, mission_name=mission_name
            )
        ).first()
        if existing_link is None:
            link = MissionContentMission()
            link.mission_content_id = content.id
            link.mission_name = mission_name
            link.mission_guid = mission.guid
            db.session.add(link)
            newly_attached.append({"hash": content.hash, "filename": content.filename})
        else:
            link_row = existing_link[0]
            if link_row.mission_guid is None:
                link_row.mission_guid = mission.guid
            already_attached.append({"hash": content.hash, "filename": content.filename})

        existing_change = db.session.execute(
            db.session.query(MissionChange).filter_by(
                content_uid=content.uid, mission_name=mission_name
            )
        ).first()
        if existing_change is None:
            change = MissionChange()
            change.isFederatedChange = False
            change.change_type = MissionChange.ADD_CONTENT
            change.content_uid = content.uid
            change.mission_name = mission_name
            change.timestamp = datetime.datetime.now(datetime.timezone.utc)
            change.creator_uid = (
                content.creator_uid
                or getattr(mission, "creator_uid", None)
                or current_user.username
            )
            change.server_time = datetime.datetime.now(datetime.timezone.utc)
            db.session.add(change)
            new_changes.append((change, content))

    db.session.commit()

    # Best-effort: publish change CoTs to subscribers. Failure here does not
    # roll back the attach — the link rows are the source of truth and any
    # missed clients will pick up the changes on next mission sync.
    if new_changes:
        try:
            rabbit_credentials = pika.PlainCredentials(
                app.config.get("OTS_RABBITMQ_USERNAME"),
                app.config.get("OTS_RABBITMQ_PASSWORD"),
            )
            rabbit_connection = pika.BlockingConnection(
                pika.ConnectionParameters(
                    host=app.config.get("OTS_RABBITMQ_SERVER_ADDRESS"),
                    credentials=rabbit_credentials,
                )
            )
            channel = rabbit_connection.channel()
            for change, content in new_changes:
                event = generate_mission_change_cot(
                    mission_name, mission, change, content=content
                )
                publish_body = json.dumps(
                    {
                        "uid": change.creator_uid,
                        "cot": tostring(event).decode("utf-8"),
                    }
                )
                channel.basic_publish(
                    "missions",
                    routing_key=f"missions.{mission_name}",
                    body=publish_body,
                )
            channel.close()
            rabbit_connection.close()
        except Exception:
            logger.error(
                "attach_mission_content: failed to publish change CoTs; "
                "DB state is committed.\n%s",
                traceback.format_exc(),
            )

    return jsonify(
        {
            "success": True,
            "mission_name": mission_name,
            "mission_guid": mission.guid,
            "attached": newly_attached,
            "already_attached": already_attached,
        }
    )
