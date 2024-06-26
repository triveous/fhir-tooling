import os
import csv
import json
import uuid
import click
import requests
import logging
import logging.config
import backoff
import base64
import magic
from datetime import datetime
from oauthlib.oauth2 import LegacyApplicationClient
from requests_oauthlib import OAuth2Session
import certifi
import ssl

try:
    import config
except ModuleNotFoundError:
    logging.error("The config.py file is missing!")
    exit()

global_access_token = ""

# This function takes in a csv file
# reads it and returns a list of strings/lines
# It ignores the first line (assumes headers)
def read_csv(csv_file):
    logging.info("Reading csv file")
    with open(csv_file, mode="r") as file:
        records = csv.reader(file, delimiter=",")
        try:
            next(records)
            all_records = []

            with click.progressbar(records, label='Progress::Reading csv ') as read_csv_progress:
                for record in read_csv_progress:
                    all_records.append(record)

            logging.info("Returning records from csv file")
            return all_records

        except StopIteration:
            logging.error("Stop iteration on empty file")

def get_access_token():
    access_token = ""
    if global_access_token:
        return global_access_token

    try:
        if config.access_token:
            # get access token from config file
            access_token = config.access_token
    except AttributeError:
        logging.debug("No access token provided, trying to use client credentials")

    if not access_token:
        # get client credentials from config file
        client_id = config.client_id
        client_secret = config.client_secret
        username = config.username
        password = config.password
        access_token_url = config.access_token_url

        oauth = OAuth2Session(client=LegacyApplicationClient(client_id=client_id))
        token = oauth.fetch_token(
            token_url=access_token_url,
            username=username,
            password=password,
            client_id=client_id,
            client_secret=client_secret
        )
        access_token = token["access_token"]

    return access_token


# This function makes the request to the provided url
# to create resources
@backoff.on_exception(backoff.expo, requests.exceptions.RequestException, max_time=180)
def post_request(request_type, payload, url):
    logging.info("Posting request")
    logging.info("Request type: " + request_type)
    logging.info("Url: " + url)
    logging.debug("Payload: " + payload)

    access_token = "Bearer " + get_access_token()
    headers = {"Content-type": "application/json", "Authorization": access_token}

    if request_type == "POST":
        return requests.post(url, data=payload, headers=headers)
    elif request_type == "PUT":
        return requests.put(url, data=payload, headers=headers)
    elif request_type == "GET":
        return requests.get(url, headers=headers)
    elif request_type == "DELETE":
        return requests.delete(url, headers=headers)
    else:
        logging.error("Unsupported request type!")


def handle_request(request_type, payload, url):
    try:
        response = post_request(request_type, payload, url)
        if response.status_code == 200 or response.status_code == 201:
            logging.info("[" + str(response.status_code) + "]" + ": SUCCESS!")

        if request_type == "GET":
            return response.text, response.status_code
        else:
            return response
    except Exception as err:
        logging.error(err)


def get_keycloak_url():
    return config.keycloak_url

# This function builds the user payload and posts it to
# the keycloak api to create a new user
# it also adds the user to the provided keycloak group
# and sets the user password
def create_user(user):
    (firstName, lastName, username, email, userId, userType, _, keycloakGroupId,
     keycloakGroupName, appId, password) = user

    with open("json_payloads/keycloak_user_payload.json") as json_file:
        payload_string = json_file.read()

    obj = json.loads(payload_string)
    obj["firstName"] = firstName
    obj["lastName"] = lastName
    obj["username"] = username
    obj["email"] = email
    obj["attributes"]["fhir_core_app_id"][0] = appId

    final_string = json.dumps(obj)
    logging.info("Creating user: " + username)
    keycloak_url = get_keycloak_url()
    r = handle_request("POST", final_string, keycloak_url + "/users")
    if r.status_code == 201:
        logging.info("User created successfully")
        new_user_location = r.headers["Location"]
        user_id = (new_user_location.split("/"))[-1]

        # add user to group
        payload = '{"id": "' + keycloakGroupId + '", "name": "' + keycloakGroupName + '"}'
        group_endpoint = user_id + "/groups/" + keycloakGroupId
        url = keycloak_url + "/users/" + group_endpoint
        logging.info("Adding user to Keycloak group: " + keycloakGroupName)
        r = handle_request("PUT", payload, url)

        # set password
        payload = '{"temporary":false,"type":"password","value":"' + password + '"}'
        password_endpoint = user_id + "/reset-password"
        url = keycloak_url + "/users/" + password_endpoint
        logging.info("Setting user password")
        r = handle_request("PUT", payload, url)

        return user_id
    else:
        return 0


# This function build the FHIR resources related to a
# new user and posts them to the FHIR api for creation
def create_user_resources(user_id, user):
    logging.info("Creating user resources")
    (firstName, lastName, username, email, id, userType,
     enableUser, keycloakGroupId, keycloakGroupName, _, password) = user

    # generate uuids
    if len(str(id).strip()) == 0:
        practitioner_uuid = str(
            uuid.uuid5(
                uuid.NAMESPACE_DNS, username + keycloakGroupId + "practitioner_uuid"
            )
        )
    else:
        practitioner_uuid = id

    group_uuid = str(
        uuid.uuid5(uuid.NAMESPACE_DNS, username + keycloakGroupId + "group_uuid")
    )
    practitioner_role_uuid = str(
        uuid.uuid5(
            uuid.NAMESPACE_DNS, username + keycloakGroupId + "practitioner_role_uuid"
        )
    )

    # get payload and replace strings
    initial_string = """{"resourceType": "Bundle","type": "transaction","meta": {"lastUpdated": ""},"entry": """
    with open("json_payloads/user_resources_payload.json") as json_file:
        payload_string = json_file.read()

    # replace the variables in payload
    ff = (
        payload_string.replace("$practitioner_uuid", practitioner_uuid)
        .replace("$keycloak_user_uuid", user_id)
        .replace("$firstName", firstName)
        .replace("$lastName", lastName)
        .replace("$email", email)
        .replace('"$enable_user"', enableUser)
        .replace("$group_uuid", group_uuid)
        .replace("$practitioner_role_uuid", practitioner_role_uuid)
    )

    obj = json.loads(ff)

    if userType.strip() == "Supervisor":
        obj[2]["resource"]["code"] = {
            "coding": [
                {
                    "system": "https://midas.iisc.ac.in/fhir/CodeSystem/practitioner-role-type",
                    "code": "super-admin",
                    "display": "Super Admin",
                }
            ]
        }
    elif userType.strip() == "Specialist":
        obj[2]["resource"]["code"] = {
            "coding": [
                {
                    "system": "https://midas.iisc.ac.in/fhir/CodeSystem/practitioner-role-type",
                    "code": "specialist",
                    "display": "Specialist",
                }
            ]
        }
    elif userType.strip() == "Senior Specialist":
        obj[2]["resource"]["code"] = {
             "coding": [
                {
                    "system": "https://midas.iisc.ac.in/fhir/CodeSystem/practitioner-role-type",
                    "code": "senior-specialist",
                    "display": "Senior Specialist",
                }
            ]
        }
    elif userType.strip() == "Reader":
        obj[2]["resource"]["code"] = {
             "coding": [
                {
                    "system": "https://midas.iisc.ac.in/fhir/CodeSystem/practitioner-role-type",
                    "code": "reader",
                    "display": "Reader",
                }
            ]
        }
    elif userType.strip() == "Front Line Worker":
        obj[2]["resource"]["code"] = {
             "coding": [
                {
                    "system": "https://midas.iisc.ac.in/fhir/CodeSystem/practitioner-role-type",
                    "code": "flw",
                    "display": "Front Line Worker",
                }
            ]
        }
    elif userType.strip() == "Site Coordinator":
        obj[2]["resource"]["code"] = {
             "coding": [
                {
                    "system": "https://midas.iisc.ac.in/fhir/CodeSystem/practitioner-role-type",
                    "code": "site-coordinator",
                    "display": "Site Coordinator",
                }
            ]
        }
    elif userType.strip() == "Site Admin":
        obj[2]["resource"]["code"] = {
             "coding": [
                {
                    "system": "https://midas.iisc.ac.in/fhir/CodeSystem/practitioner-role-type",
                    "code": "site-admin",
                    "display": "Site Admin",
                }
            ]
        }
    else:
        del obj[2]["resource"]["code"]
    ff = json.dumps(obj, indent=4)

    payload = initial_string + ff + "}"
    return payload


# custom extras for organizations
def organization_extras(resource, payload_string):
    try:
        _, orgActive, *_ = resource
    except ValueError:
        orgActive = "true"

    try:
        payload_string = payload_string.replace("$active", orgActive)
    except IndexError:
        payload_string = payload_string.replace("$active", "true")
    return payload_string


def identify_coding_object_index(array, current_system):
    for index, value in enumerate(array):
        list_of_systems = value["coding"][0]["system"]
        if current_system in list_of_systems:
            return index


def check_parent_admin_level(locationParentId):
    base_url = get_base_url()
    resource_url = "/".join([base_url, "Location", locationParentId])
    response = handle_request("GET", "", resource_url)
    obj = json.loads(response[0])
    if "type" in obj:
        response_type = obj["type"]
        current_system = "administrative-level"
        if current_system:
            index = identify_coding_object_index(response_type, current_system)
            if index >= 0:
                code = obj["type"][index]["coding"][0]["code"]
                admin_level = str(int(code) + 1)
                return admin_level
        else:
            return None
    else:
        return None


# custom extras for locations
def location_extras(resource, payload_string):
    try:
        (locationName, *_, locationParentName, locationParentId, locationType, locationTypeCode,
         locationAdminLevel, locationPhysicalType, locationPhysicalTypeCode, longitude, latitude) = resource
    except ValueError:
        locationParentName = "parentName"
        locationParentId = "ParentId"
        locationType = "type"
        locationTypeCode = "typeCode"
        locationAdminLevel = "adminLevel"
        locationPhysicalType = "physicalType"
        locationPhysicalTypeCode = "physicalTypeCode"
        longitude = "longitude"

    try:
        if locationParentName and locationParentName != "parentName":
            payload_string = payload_string.replace("$parentName", locationParentName).replace(
                "$parentID", locationParentId
            )
        else:
            obj = json.loads(payload_string)
            del obj["resource"]["partOf"]
            payload_string = json.dumps(obj, indent=4)
    except IndexError:
        obj = json.loads(payload_string)
        del obj["resource"]["partOf"]
        payload_string = json.dumps(obj, indent=4)

    try:
        if len(locationType.strip()) > 0 and locationType != "type":
            payload_string = payload_string.replace("$t_display", locationType)
        if len(locationTypeCode.strip()) > 0 and locationTypeCode != "typeCode":
            payload_string = payload_string.replace("$t_code", locationTypeCode)
        else:
            obj = json.loads(payload_string)
            payload_type = obj["resource"]["type"]
            current_system = "location-type"
            index = identify_coding_object_index(payload_type, current_system)
            if index >= 0:
                del obj["resource"]["type"][index]
                payload_string = json.dumps(obj, indent=4)
    except IndexError:
        obj = json.loads(payload_string)
        payload_type = obj["resource"]["type"]
        current_system = "location-type"
        index = identify_coding_object_index(payload_type, current_system)
        if index >= 0:
            del obj["resource"]["type"][index]
            payload_string = json.dumps(obj, indent=4)

    try:
        if len(locationAdminLevel.strip()) > 0 and locationAdminLevel != "adminLevel":
            payload_string = payload_string.replace("$adminLevelCode", locationAdminLevel)
        else:
            if locationAdminLevel in resource:
                admin_level = check_parent_admin_level(locationParentId)
                if admin_level:
                    payload_string = payload_string.replace("$adminLevelCode", admin_level)
                else:
                    obj = json.loads(payload_string)
                    obj_type = obj["resource"]["type"]
                    current_system = "administrative-level"
                    index = identify_coding_object_index(obj_type, current_system)
                    del obj["resource"]["type"][index]
                    payload_string = json.dumps(obj, indent=4)
            else:
                obj = json.loads(payload_string)
                obj_type = obj["resource"]["type"]
                current_system = "administrative-level"
                index = identify_coding_object_index(obj_type, current_system)
                del obj["resource"]["type"][index]
                payload_string = json.dumps(obj, indent=4)
    except IndexError:
        if locationAdminLevel in resource:
            admin_level = check_parent_admin_level(locationParentId)
            if admin_level:
                payload_string = payload_string.replace("$adminLevelCode", admin_level)
            else:
                obj = json.loads(payload_string)
                obj_type = obj["resource"]["type"]
                current_system = "administrative-level"
                index = identify_coding_object_index(obj_type, current_system)
                del obj["resource"]["type"][index]
                payload_string = json.dumps(obj, indent=4)
        else:
            obj = json.loads(payload_string)
            obj_type = obj["resource"]["type"]
            current_system = "administrative-level"
            index = identify_coding_object_index(obj_type, current_system)
            del obj["resource"]["type"][index]
            payload_string = json.dumps(obj, indent=4)

    try:
        if len(locationPhysicalType.strip()) > 0 and locationPhysicalType != "physicalType":
            payload_string = payload_string.replace("$pt_display", locationPhysicalType)
        if len(locationPhysicalTypeCode.strip()) > 0 and locationPhysicalTypeCode != "physicalTypeCode":
            payload_string = payload_string.replace("$pt_code", locationPhysicalTypeCode)
        else:
            obj = json.loads(payload_string)
            del obj["resource"]["physicalType"]
            payload_string = json.dumps(obj, indent=4)
    except IndexError:
        obj = json.loads(payload_string)
        del obj["resource"]["physicalType"]
        payload_string = json.dumps(obj, indent=4)

    try:
        if longitude and longitude != "longitude":
            payload_string = payload_string.replace('"$longitude"', longitude).replace(
                '"$latitude"', latitude)
        else:
            obj = json.loads(payload_string)
            del obj["resource"]["position"]
            payload_string = json.dumps(obj, indent=4)
    except IndexError:
        obj = json.loads(payload_string)
        del obj["resource"]["position"]
        payload_string = json.dumps(obj, indent=4)

    return payload_string


# custom extras for careTeams
def care_team_extras(
        resource, payload_string, ftype
):
    orgs_list = []
    participant_list = []
    elements = []
    elements2 = []

    try:
        *_, organizations, participants = resource
    except ValueError:
        organizations = "organizations"
        participants = "participants"

    if organizations and organizations != "organizations":
        elements = organizations.split("|")
    else:
        logging.info("No organizations")

    if participants and participants != "participants":
        elements2 = participants.split("|")
    else:
        logging.info("No participants")

    if "orgs" in ftype:
        for org in elements:
            y = {}
            x = org.split(":")
            y["reference"] = "Organization/" + str(x[0])
            y["display"] = str(x[1])
            orgs_list.append(y)

            z = {
                "role": [
                    {
                        "coding": [
                            {
                                "system": "http://snomed.info/sct",
                                "code": "394730007",
                                "display": "Healthcare related organization",
                            }
                        ]
                    }
                ],
                "member": {},
            }
            z["member"]["reference"] = "Organization/" + str(x[0])
            z["member"]["display"] = str(x[1])
            participant_list.append(z)

        if len(participant_list) > 0:
            obj = json.loads(payload_string)
            obj["resource"]["participant"] = participant_list
            obj["resource"]["managingOrganization"] = orgs_list
            payload_string = json.dumps(obj)

    if "users" in ftype:
        if len(elements2) > 0:
            elements = elements2
        for user in elements:
            y = {"member": {}}
            x = user.split(":")
            y["member"]["reference"] = "Practitioner/" + str(x[0])
            y["member"]["display"] = str(x[1])
            participant_list.append(y)

        if len(participant_list) > 0:
            obj = json.loads(payload_string)
            obj["resource"]["participant"] = participant_list
            payload_string = json.dumps(obj)

    return payload_string


def extract_matches(resource_list):
    teamMap = {}
    with click.progressbar(resource_list, label='Progress::Extract matches ') as extract_progress:
        for resource in extract_progress:
            group_name, group_id, item_name, item_id = resource
            if group_id.strip() and item_id.strip():
                if group_id not in teamMap.keys():
                    teamMap[group_id] = [item_id + ":" + item_name]
                else:
                    teamMap[group_id].append(item_id + ":" + item_name)
            else:
                logging.error("Missing required id: Skipping " + str(resource))
    return teamMap


def build_assign_payload(rows, resource_type):
    initial_string = """{"resourceType": "Bundle","type": "transaction","entry": [ """
    final_string = ""
    for row in rows:
        practitioner_name, practitioner_id, organization_name, organization_id = row

        # check if already exists
        base_url = get_base_url()
        check_url = (base_url + "/" + resource_type + "/_search?_count=1&practitioner=Practitioner/"
                     + practitioner_id)
        response = handle_request("GET", "", check_url)
        json_response = json.loads(response[0])

        if json_response["total"] == 1:
            logging.info("Updating existing resource")
            resource = json_response["entry"][0]["resource"]

            try:
                resource["organization"]["reference"] = "Organization/" + organization_id
                resource["organization"]["display"] = organization_name
            except KeyError:
                org = {
                    "organization": {
                        "reference": "Organization/" + organization_id,
                        "display": organization_name
                    }
                }
                resource.update(org)

            version = resource["meta"]["versionId"]
            practitioner_role_id = resource["id"]
            del resource["meta"]

        elif json_response["total"] == 0:
            logging.info("Creating a new resource")

            # generate a new id
            new_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, practitioner_id + organization_id))

            with open("json_payloads/practitioner_organization_payload.json") as json_file:
                payload_string = json_file.read()

            # replace the variables in payload
            payload_string = (
                payload_string.replace("$id", new_id)
                .replace("$practitioner_id", practitioner_id)
                .replace("$practitioner_name", practitioner_name)
                .replace("$organization_id", organization_id)
                .replace("$organization_name", organization_name)
            )
            version = "1"
            practitioner_role_id = new_id
            resource = json.loads(payload_string)

        else:
            raise ValueError ("The number of practitioner references should only be 0 or 1")

        payload = {
            "request": {
                "method": "PUT",
                "url": resource_type + "/" + practitioner_role_id,
                "ifMatch": version
            },
            "resource": resource
        }
        full_string = json.dumps(payload, indent=4)
        final_string = final_string + full_string + ","

    final_string = initial_string + final_string[:-1] + " ] } "
    return final_string


def get_org_name(key, resource_list):
    for x in resource_list:
        if x[1] == key:
            org_name = x[0]

    return org_name


def build_org_affiliation(resources, resource_list):
    fp = """{"resourceType": "Bundle","type": "transaction","entry": [ """

    with open("json_payloads/organization_affiliation_payload.json") as json_file:
        payload_string = json_file.read()

    with click.progressbar(resources, label='Progress::Build payload ') as build_progress:
        for key in build_progress:
            rp = ""
            unique_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, key))
            org_name = get_org_name(key, resource_list)

            rp = (
                payload_string.replace("$unique_uuid", unique_uuid)
                .replace("$identifier_uuid", unique_uuid)
                .replace("$version", "1")
                .replace("$orgID", key)
                .replace("$orgName", org_name)
            )

            locations = []
            for x in resources[key]:
                y = {}
                z = x.split(":")
                y["reference"] = "Location/" + str(z[0])
                y["display"] = str(z[1])
                locations.append(y)

            obj = json.loads(rp)
            obj["resource"]["location"] = locations
            rp = json.dumps(obj)

            fp = fp + rp + ","

    fp = fp[:-1] + " ] } "
    return fp


# This function is used to Capitalize the 'resource_type'
# and remove the 's' at the end, a version suitable with the API
def get_valid_resource_type(resource_type):
    logging.debug("Modify the string resource_type")
    modified_resource_type = resource_type[0].upper() + resource_type[1:-1]
    return modified_resource_type


# This function gets the current resource version from the API
def get_resource(resource_id, resource_type):
    resource_type = get_valid_resource_type(resource_type)
    resource_url = "/".join([config.fhir_base_url, resource_type, resource_id])
    response = handle_request("GET", "", resource_url)
    return json.loads(response[0])["meta"]["versionId"] if response[1] == 200 else "0"


# This function builds a json payload
# which is posted to the api to create resources
def build_payload(resource_type, resources, resource_payload_file):
    logging.info("Building request payload")
    initial_string = """{"resourceType": "Bundle","type": "transaction","entry": [ """
    final_string = " "
    with open(resource_payload_file) as json_file:
        payload_string = json_file.read()

    with click.progressbar(resources, label='Progress::Building payload ') as build_payload_progress:
        for resource in build_payload_progress:
            logging.info("\t")

            try:
                name, status, method, id, *_ = resource
            except ValueError:
                name = resource[0]
                status = "" if len(resource) == 1 else resource[1]
                method = "create"
                id = str(uuid.uuid5(uuid.NAMESPACE_DNS, name))

            try:
                if method == "create":
                    version = "1"
                    if len(id.strip()) > 0:
                        # use the provided id
                        unique_uuid = id.strip()
                        identifier_uuid = id.strip()
                    else:
                        # generate a new uuid
                        unique_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, name))
                        identifier_uuid = unique_uuid
            except IndexError:
                # default if method is not provided
                unique_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, name))
                identifier_uuid = unique_uuid
                version = "1"

            try:
                if method == "update":
                    if len(id.strip()) > 0:
                        version = get_resource(id, resource_type)
                        if version != "0":
                            # use the provided id
                            unique_uuid = id.strip()
                            identifier_uuid = id.strip()
                        else:
                            logging.info("Failed to get resource!")
                            raise ValueError("Trying to update a Non-existent resource")
                    else:
                        logging.info("The id is required!")
                        raise ValueError("The id is required to update a resource")
            except IndexError:
                raise ValueError("The id is required to update a resource")

            # ps = payload_string
            ps = (
                payload_string.replace("$name", name)
                .replace("$unique_uuid", unique_uuid)
                .replace("$identifier_uuid", identifier_uuid)
                .replace("$version", version)
            )

            try:
                ps = ps.replace("$status", status)
            except IndexError:
                ps = ps.replace("$status", "active")

            if resource_type == "organizations":
                ps = organization_extras(resource, ps)
            elif resource_type == "locations":
                ps = location_extras(resource, ps)
            elif resource_type == "careTeams":
                ps = care_team_extras(resource, ps, "orgs & users")

            final_string = final_string + ps + ","

    final_string = initial_string + final_string[:-1] + " ] } "
    return final_string


def confirm_keycloak_user(user):
    # Confirm that the keycloak user details are as expected
    user_username = str(user[2]).strip()
    user_email = str(user[3]).strip()
    keycloak_url = get_keycloak_url()
    response = handle_request(
        "GET", "", keycloak_url + "/users?exact=true&username=" + user_username
    )
    logging.debug(response)
    json_response = json.loads(response[0])

    try:
        response_email = json_response[0]["email"]
    except IndexError:
        response_email = ""

    try:
        response_username = json_response[0]["username"]
    except IndexError:
        logging.error("Skipping user: " + str(user))
        logging.error("Username not found!")
        return 0

    if response_username != user_username:
        logging.error("Skipping user: " + str(user))
        logging.error("Username does not match")
        return 0

    if len(response_email) > 0 and response_email != user_email:
        logging.error("Email does not match for user: " + str(user))

    keycloak_id = json_response[0]["id"]
    logging.info("User confirmed with id: " + keycloak_id)
    return keycloak_id


def confirm_practitioner(user, user_id):
    practitioner_uuid = str(user[4]).strip()
    base_url = get_base_url()
    if not practitioner_uuid:
        # If practitioner uuid not provided in csv, check if any practitioners exist linked to the keycloak user_id
        r = handle_request(
            "GET", "", base_url + "/Practitioner?identifier=" + user_id
        )
        json_r = json.loads(r[0])
        counter = json_r["total"]
        if counter > 0:
            logging.info(
                str(counter) + " Practitioner(s) exist, linked to the provided user"
            )
            return True
        else:
            return False

    r = handle_request(
        "GET", "", base_url + "/Practitioner/" + practitioner_uuid
    )

    if r[1] == 404:
        logging.info("Practitioner does not exist, proceed to creation")
        return False
    else:
        try:
            json_r = json.loads(r[0])
            identifiers = json_r["identifier"]
            keycloak_id = 0
            for id in identifiers:
                if id["use"] == "secondary":
                    keycloak_id = id["value"]

            if str(keycloak_id) == user_id:
                logging.info(
                    "The Keycloak user and Practitioner are linked as expected"
                )
                return True
            else:
                logging.error(
                    "The Keycloak user and Practitioner are not linked as expected"
                )
                return True

        except Exception as err:
            logging.error("Error occured trying to find Practitioner: " + str(err))
            return True


def create_roles(role_list, roles_max):
    for role in role_list:
        current_role = str(role[0])
        logging.info("The current role is: " + current_role)

        # check if role already exists
        role_response = handle_request(
            "GET", "", config.keycloak_url + "/roles/" + current_role
        )
        logging.debug(role_response)
        if current_role in role_response[0]:
            logging.error("A role already exists with the name " + current_role)
        else:
            role_payload = '{"name": "' + current_role + '"}'
            create_role = handle_request(
                "POST", role_payload, config.keycloak_url + "/roles"
            )
            if create_role.status_code == 201:
                logging.info("Successfully created role: " + current_role)

        try:
            # check if role has composite roles
            if role[1]:
                logging.debug("Role has composite roles")
                # get roled id
                full_role = handle_request(
                    "GET", "", config.keycloak_url + "/roles/" + current_role
                )
                json_resp = json.loads(full_role[0])
                role_id = json_resp["id"]
                logging.debug("roleId: " + str(role_id))

                # get all available roles
                available_roles = handle_request(
                    "GET",
                    "",
                    config.keycloak_url
                    + "/ui-ext/available-roles/roles/"
                    + role_id
                    + "?first=0&max="
                    + str(roles_max)
                    + "&search=",
                )
                json_roles = json.loads(available_roles[0])
                logging.debug("json_roles: " + str(json_roles))

                rolesMap = {}

                for jrole in json_roles:
                    # remove client and clientId, then rename role to name
                    # to build correct payload
                    del jrole["client"]
                    del jrole["clientId"]
                    jrole["name"] = jrole["role"]
                    del jrole["role"]
                    rolesMap[str(jrole["name"])] = jrole

                associated_roles = str(role[2])
                logging.debug("Associated roles: " + associated_roles)
                associated_role_array = associated_roles.split("|")
                arr = []
                for arole in associated_role_array:
                    if arole in rolesMap.keys():
                        arr.append(rolesMap[arole])
                    else:
                        logging.error("Role " + arole + "does not exist")

                payload_arr = json.dumps(arr)
                handle_request(
                    "POST",
                    payload_arr,
                    config.keycloak_url + "/roles-by-id/" + role_id + "/composites",
                )

        except IndexError:
            pass


def get_group_id(group):
    # check if group exists
    all_groups = handle_request("GET", "", config.keycloak_url + "/groups")
    json_groups = json.loads(all_groups[0])
    group_obj = {}

    for agroup in json_groups:
        group_obj[agroup["name"]] = agroup

    if group in group_obj.keys():
        gid = str(group_obj[group]["id"])
        logging.info("Group already exists with id : " + gid)
        return gid

    else:
        logging.info("Group does not exists, lets create it")
        # create the group
        create_group_payload = '{"name":"' + group + '"}'
        handle_request("POST", create_group_payload, config.keycloak_url + "/groups")
        
        logging.info("Group {} created",group)
        return get_group_id(group)


def assign_group_roles(role_list, group, roles_max):
    group_id = get_group_id(group)
    logging.debug("The groupID is: " + group_id)

    # get available roles
    available_roles_for_group = handle_request(
        "GET",
        "",
        config.keycloak_url
        + "/groups/"
        + group_id
        + "/role-mappings/realm/available?first=0&max="
        + str(roles_max),
    )
    json_roles = json.loads(available_roles_for_group[0])
    role_obj = {}

    for j in json_roles:
        role_obj[j["name"]] = j

    assign_payload = []
    for r in role_list:
        if r[0] in role_obj.keys():
            assign_payload.append(role_obj[r[0]])

    json_assign_payload = json.dumps(assign_payload)
    handle_request(
        "POST",
        json_assign_payload,
        config.keycloak_url + "/groups/" + group_id + "/role-mappings/realm",
    )
    logging.info("Role added to group " + group)


def delete_resource(resource_type, resource_id, cascade):
    if cascade:
        cascade = "?_cascade=delete"
    else:
        cascade = ""

    resource_url = "/".join(
        [config.fhir_base_url, resource_type, resource_id + cascade]
    )
    r = handle_request("DELETE", "", resource_url)
    logging.info(r.text)


def clean_duplicates(users, cascade_delete):
    for user in users:
        # get keycloak user uuid
        username = str(user[2].strip())
        user_details = handle_request(
            "GET", "", config.keycloak_url + "/users?exact=true&username=" + username
        )
        obj = json.loads(user_details[0])
        keycloak_uuid = obj[0]["id"]

        # get Practitioner(s)
        r = handle_request(
            "GET",
            "",
            config.fhir_base_url + "/Practitioner?identifier=" + keycloak_uuid,
        )
        practitioner_details = json.loads(r[0])
        count = practitioner_details["total"]

        try:
            practitioner_uuid_provided = str(user[4].strip())
        except IndexError:
            practitioner_uuid_provided = None

        if practitioner_uuid_provided:
            if count == 1:
                practitioner_uuid_returned = practitioner_details["entry"][0][
                    "resource"
                ]["id"]
                # confirm the uuid matches the one provided in csv
                if practitioner_uuid_returned == practitioner_uuid_provided:
                    logging.info("User " + username + " ok!")
                else:
                    logging.error(
                        "User "
                        + username
                        + "has 1 Practitioner but it does not match the provided uuid"
                    )
            elif count > 1:
                for x in practitioner_details["entry"]:
                    p_uuid = x["resource"]["id"]
                    if practitioner_uuid_provided == p_uuid:
                        # This is the correct resource, so skip it
                        continue
                    else:
                        logging.info(
                            "Deleting practitioner resource with uuid: " + str(p_uuid)
                        )
                        delete_resource("Practitioner", p_uuid, cascade_delete)
            else:
                # count is less than 1
                logging.info("No Practitioners found")


# Create a csv file and initialize the CSV writer
def write_csv(data, resource_type, fieldnames):
    logging.info("Writing to csv file")
    path = 'csv/exports'
    if not os.path.exists(path):
        os.makedirs(path)

    current_time = datetime.now().strftime("%Y-%m-%d-%H-%M")
    csv_file = f"{path}/{current_time}-export_{resource_type}.csv"
    with open(csv_file, 'w', newline='') as file:
        csv_writer = csv.writer(file)
        csv_writer.writerow(fieldnames)
        with click.progressbar(data, label='Progress:: Writing csv') as write_csv_progress:
            for row in write_csv_progress:
                csv_writer.writerow(row)
    return csv_file


def get_base_url():
    return config.fhir_base_url


# This function exports resources from the API to a csv file
def export_resources_to_csv(resource_type, parameter, value, limit):
    base_url = get_base_url()
    resource_url = "/".join([str(base_url), resource_type])
    if len(parameter) > 0:
        resource_url = (
                resource_url + "?" + parameter + "=" + value + "&_count=" + str(limit)
        )
    response = handle_request("GET", "", resource_url)
    if response[1] == 200:
        resources = json.loads(response[0])
        data = []
        try:
            if resources["entry"]:
                if resource_type == "Location":
                    elements = ["name", "status", "method", "id", "identifier", "parentName", "parentID", "type",
                                "typeCode",
                                "physicalType", "physicalTypeCode"]
                elif resource_type == "Organization":
                    elements = ["name", "active", "method", "id", "identifier"]
                elif resource_type == "CareTeam":
                    elements = ["name", "status", "method", "id", "identifier", "organizations", "participants"]
                else:
                    elements = []
                with click.progressbar(resources["entry"],
                                       label='Progress:: Extracting resource') as extract_resources_progress:
                    for x in extract_resources_progress:
                        rl = []
                        orgs_list = []
                        participants_list = []
                        for element in elements:
                            try:
                                if element == "method":
                                    value = "update"
                                elif element == "active":
                                    value = x["resource"]["active"]
                                elif element == "identifier":
                                    value = x["resource"]["identifier"][0]["value"]
                                elif element == "organizations":
                                    organizations = x["resource"]["managingOrganization"]
                                    for index, value in enumerate(organizations):
                                        reference = x["resource"]["managingOrganization"][index]["reference"]
                                        new_reference = reference.split("/", 1)[1]
                                        display = x["resource"]["managingOrganization"][index]["display"]
                                        organization = ":".join([new_reference, display])
                                        orgs_list.append(organization)
                                    string = "|".join(map(str, orgs_list))
                                    value = string
                                elif element == "participants":
                                    participants = x["resource"]["participant"]
                                    for index, value in enumerate(participants):
                                        reference = x["resource"]["participant"][index]["member"]["reference"]
                                        new_reference = reference.split("/", 1)[1]
                                        display = x["resource"]["participant"][index]["member"]["display"]
                                        participant = ":".join([new_reference, display])
                                        participants_list.append(participant)
                                    string = "|".join(map(str, participants_list))
                                    value = string
                                elif element == "parentName":
                                    value = x["resource"]["partOf"]["display"]
                                elif element == "parentID":
                                    reference = x["resource"]["partOf"]["reference"]
                                    value = reference.split("/", 1)[1]
                                elif element == "type":
                                    value = x["resource"]["type"][0]["coding"][0]["display"]
                                elif element == "typeCode":
                                    value = x["resource"]["type"][0]["coding"][0]["code"]
                                elif element == "physicalType":
                                    value = x["resource"]["physicalType"]["coding"][0]["display"]
                                elif element == "physicalTypeCode":
                                    value = x["resource"]["physicalType"]["coding"][0]["code"]
                                else:
                                    value = x["resource"][element]
                            except KeyError:
                                value = ""
                            rl.append(value)
                        data.append(rl)
                write_csv(data, resource_type, elements)
                logging.info("Successfully written to csv")
            else:
                logging.info("No entry found")
        except KeyError:
            logging.info("No Resources Found")
    else:
        logging.error(f"Failed to retrieve resource. Status code: {response[1]} response: {response[0]}")


def encode_image(image_file):
    with open(image_file, 'rb') as image:
        image_b64_data = base64.b64encode(image.read())
    return image_b64_data


# This function takes in the source url of an image, downloads it, encodes it,
# and saves it as a Binary resource. It returns the id of the Binary resource if
# successful and 0 if failed
def save_image(image_source_url):
    headers = {"Authorization": "Bearer " + config.product_access_token}
    data = requests.get(url=image_source_url, headers=headers)
    if data.status_code == 200:
        with open('images/image_file', 'wb') as image_file:
            image_file.write(data.content)

        # get file type
        mime = magic.Magic(mime=True)
        file_type = mime.from_file('images/image_file')

        encoded_image = encode_image('images/image_file')
        resource_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, image_source_url))
        payload = {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [{
                "request": {
                    "method": "PUT",
                    "url": "Binary/" + resource_id,
                    "ifMatch": "1"
                },
                "resource": {
                    "resourceType": "Binary",
                    "id": resource_id,
                    "contentType": file_type,
                    "data": str(encoded_image)
                }
            }]
        }
        payload_string = json.dumps(payload, indent=4)
        response = handle_request("POST", payload_string, get_base_url())
        if response.status_code == 200:
            logging.info("Binary resource created successfully")
            logging.info(response.text)
            return resource_id
        else:
            logging.error("Error while creating Binary resource")
            logging.error(response.text)
            return 0
    else:
        logging.error("Error while attempting to retrieve image")
        logging.error(data)
        return 0


class ResponseFilter(logging.Filter):
    def __init__(self, param=None):
        self.param = param

    def filter(self, record):
        if self.param is None:
            allow = True
        else:
            allow = self.param in record.msg
        return allow


LOGGING = {
    'version': 1,
    'filters': {
        'custom-filter': {
            '()': ResponseFilter,
            'param': 'final-response',
        }
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'filters': ['custom-filter']
        }
    },
    'root': {
        'level': 'INFO',
        'handlers': ['console']
    },
}


@click.command()
@click.option("--csv_file", required=False)
@click.option("--access_token", required=False)
@click.option("--resource_type", required=False)
@click.option("--assign", required=False)
@click.option("--setup", required=False)
@click.option("--group", required=False)
@click.option("--roles_max", required=False, default=500)
@click.option("--cascade_delete", required=False, default=False)
@click.option("--only_response", required=False)
@click.option("--log_level", type=click.Choice(["DEBUG", "INFO", "ERROR"], case_sensitive=False))
@click.option("--export_resources", required=False)
@click.option("--parameter", required=False, default="_lastUpdated")
@click.option("--value", required=False, default="gt2023-01-01")
@click.option("--limit", required=False, default=1000)
def main(
    csv_file, access_token, resource_type, assign, setup, group, roles_max, cascade_delete, only_response, log_level,
    export_resources, parameter, value, limit
):
    if log_level == "DEBUG":
        logging.basicConfig(filename='importer.log', encoding='utf-8', level=logging.DEBUG)
    elif log_level == "INFO":
        logging.basicConfig(filename='importer.log', encoding='utf-8', level=logging.INFO)
    elif log_level == "ERROR":
        logging.basicConfig(filename='importer.log', encoding='utf-8', level=logging.ERROR)
    logging.getLogger().addHandler(logging.StreamHandler())

    if only_response:
        logging.config.dictConfig(LOGGING)

    start_time = datetime.now()
    logging.info("Start time: " + start_time.strftime("%H:%M:%S"))

    if export_resources == "True":
        logging.info("Starting export...")
        logging.info("Exporting " + resource_type)
        export_resources_to_csv(resource_type, parameter, value, limit)
        exit()

    # set access token
    if access_token:
        global global_access_token
        global_access_token = access_token

    final_response = ""

    logging.info("Starting csv import...")
    resource_list = read_csv(csv_file)
    if resource_list:
        if resource_type == "users":
            logging.info("Processing users")
            with click.progressbar(resource_list, label="Progress:Processing users ") as process_user_progress:
                for user in process_user_progress:
                    user_id = create_user(user)
                    if user_id == 0:
                        # user was not created above, check if it already exists
                        user_id = confirm_keycloak_user(user)
                    if user_id != 0:
                        # user_id has been retrieved
                        # check practitioner
                        practitioner_exists = confirm_practitioner(user, user_id)
                        if not practitioner_exists:
                            payload = create_user_resources(user_id, user)
                            final_response = handle_request("POST", payload, config.fhir_base_url)
                    logging.info("Processing complete!")
        elif resource_type == "locations":
            logging.info("Processing locations")
            json_payload = build_payload(
                "locations", resource_list, "json_payloads/locations_payload.json"
            )
            final_response = handle_request("POST", json_payload, config.fhir_base_url)
            logging.info("Processing complete!")
        elif resource_type == "organizations":
            logging.info("Processing organizations")
            json_payload = build_payload(
                "organizations",
                resource_list,
                "json_payloads/organizations_payload.json",
            )
            final_response = handle_request("POST", json_payload, config.fhir_base_url)
            logging.info("Processing complete!")
        elif resource_type == "careTeams":
            logging.info("Processing CareTeams")
            json_payload = build_payload(
                "careTeams", resource_list, "json_payloads/careteams_payload.json"
            )
            final_response = handle_request("POST", json_payload, config.fhir_base_url)
            logging.info("Processing complete!")
        elif assign == "organizations-Locations":
            logging.info("Assigning Organizations to Locations")
            matches = extract_matches(resource_list)
            json_payload = build_org_affiliation(matches, resource_list)
            final_response = handle_request("POST", json_payload, config.fhir_base_url)
            logging.info("Processing complete!")
        elif assign == "users-organizations":
            logging.info("Assigning practitioner to Organization")
            json_payload = build_assign_payload(resource_list, "PractitionerRole")
            final_response = handle_request("POST", json_payload, config.fhir_base_url)
            logging.info("Processing complete!")
        elif setup == "roles":
            logging.info("Setting up keycloak roles")
            create_roles(resource_list, roles_max)
            if group:
                assign_group_roles(resource_list, group, roles_max)
            logging.info("Processing complete")
            return
        elif setup == "clean_duplicates":
            logging.info("=========================================")
            logging.info(
                "You are about to clean/delete Practitioner resources on the HAPI server"
            )
            click.confirm("Do you want to continue?", abort=True)
            clean_duplicates(resource_list, cascade_delete)
            logging.info("Processing complete!")
        else:
            logging.error("Unsupported request!")
    else:
        logging.error("Empty csv file!")

    logging.info("{ \"final-response\": " + final_response.text + "}")

    end_time = datetime.now()
    logging.info("End time: " + end_time.strftime("%H:%M:%S"))
    total_time = end_time - start_time
    logging.info("Total time: " + str(total_time.total_seconds()) + " seconds")

if __name__ == "__main__":
    main()
