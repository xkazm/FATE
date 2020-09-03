#
#  Copyright 2019 The FATE Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import os
import shutil
from datetime import datetime

from fate_flow.db.db_models import DB, MachineLearningModelInfo as MLModel
from fate_flow.pipelined_model import pipelined_model
from fate_arch.common.base_utils import json_loads, json_dumps
from fate_arch.common.file_utils import get_project_base_directory
from fate_flow.utils import model_utils
from federatedml.protobuf.model_migrate.model_migrate import model_migration


def gen_model_file_path(model_id, model_version):
    return os.path.join(get_project_base_directory(), "model_local_cache", model_id, model_version)


def compare_roles(request_conf_roles: dict, run_time_conf_roles: dict):
    if request_conf_roles.keys() == run_time_conf_roles.keys():
        varify_format = True
        varify_equality = True
        for key in request_conf_roles.keys():
            varify_format = varify_format and (len(request_conf_roles[key]) == len(run_time_conf_roles[key])) and (isinstance(request_conf_roles[key], list))
            request_conf_roles_set = set(str(item) for item in request_conf_roles[key])
            run_time_conf_roles_set = set(str(item) for item in run_time_conf_roles[key])
            varify_equality = varify_equality and (request_conf_roles_set == run_time_conf_roles_set)
        if not varify_format:
            raise Exception("The structure of roles data of local configuration is different from "
                            "model runtime configuration's. Migration aborting.")
        else:
            return varify_equality
    raise Exception("The structure of roles data of local configuration is different from "
                    "model runtime configuration's. Migration aborting.")


def import_from_files(config: dict):
    model = pipelined_model.PipelinedModel(model_id=config["model_id"],
                                           model_version=config["model_version"])
    if config['force']:
        model.force = True
    model.unpack_model(config["file"])


def import_from_db(config: dict):
    model_path = gen_model_file_path(config["model_id"], config["model_version"])
    if config['force']:
        os.rename(model_path, model_path + '_backup_{}'.format(datetime.now().strftime('%Y%m%d%H%M')))


def migration(config_data: dict):
    try:
        party_model_id = model_utils.gen_party_model_id(model_id=config_data["model_id"],
                                                        role=config_data["local"]["role"],
                                                        party_id=config_data["local"]["party_id"])
        model = pipelined_model.PipelinedModel(model_id=party_model_id,
                                               model_version=config_data["model_version"])
        if not model.exists():
            raise Exception("Can not found {} {} model local cache".format(config_data["model_id"],
                                                                           config_data["model_version"]))
        with DB.connection_context():
            if MLModel.get_or_none(MLModel.f_model_version == config_data["unify_model_version"]):
                raise Exception("Unify model version {} has been occupied in database. "
                                "Please choose another unify model version and try again.".format(
                    config_data["unify_model_version"]))

        model_data = model.collect_models(in_bytes=True)
        if "pipeline.pipeline:Pipeline" not in model_data:
            raise Exception("Can not found pipeline file in model.")

        migrate_model = pipelined_model.PipelinedModel(model_id=model_utils.gen_party_model_id(model_id=model_utils.gen_model_id(config_data["migrate_role"]),
                                                                                               role=config_data["local"]["role"],
                                                                                               party_id=config_data["local"]["migrate_party_id"]),
                                                       model_version=config_data["unify_model_version"])

        # migrate_model.create_pipelined_model()
        shutil.copytree(src=model.model_path, dst=migrate_model.model_path)

        pipeline = migrate_model.read_component_model('pipeline', 'pipeline')['Pipeline']

        # Utilize Pipeline_model collect model data. And modify related inner information of model
        train_runtime_conf = json_loads(pipeline.train_runtime_conf)
        train_runtime_conf["role"] = config_data["migrate_role"]
        train_runtime_conf["job_parameters"]["model_id"] = model_utils.gen_model_id(train_runtime_conf["role"])
        train_runtime_conf["job_parameters"]["model_version"] = migrate_model.model_version
        train_runtime_conf["initiator"] = config_data["migrate_initiator"]

        # update pipeline.pb file
        pipeline.train_runtime_conf = json_dumps(train_runtime_conf, byte=True)
        pipeline.model_id = bytes(train_runtime_conf["job_parameters"]["model_id"], "utf-8")
        pipeline.model_version = bytes(train_runtime_conf["job_parameters"]["model_version"], "utf-8")

        # save updated pipeline.pb file
        migrate_model.save_pipeline(pipeline)
        shutil.copyfile(os.path.join(migrate_model.model_path, "pipeline.pb"),
                        os.path.join(migrate_model.model_path, "variables", "data", "pipeline", "pipeline", "Pipeline"))

        # modify proto
        secureboost_dict = migrate_model.read_component_model('secureboost_0', 'train')

        modified_buffer = model_migration(model_contents=secureboost_dict,
                                          module_name='HeteroSecureBoost',
                                          old_guest_list=config_data['role']['guest'],
                                          new_guest_list=config_data['migrate_role']['guest'],
                                          old_host_list=config_data['role']['host'],
                                          new_host_list=config_data['migrate_role']['host'])

        migrate_model.save_component_model('secureboost_0', 'HeteroSecureBoostingTreeGuestParam',
                                           'train', modified_buffer)

        with DB.connection_context():
            MLModel.create(
                f_role=config_data["local"]["role"],
                f_party_id=config_data["local"]["migrate_party_id"],
                f_roles=config_data["migrate_role"],
                f_job_id=train_runtime_conf["job_parameters"]["model_version"],
                f_model_id=train_runtime_conf["job_parameters"]["model_id"],
                f_model_version=train_runtime_conf["job_parameters"]["model_version"],
                f_initiator_role=config_data["migrate_initiator"]["role"],
                f_initiator_party_id=config_data["migrate_initiator"]["party_id"],
                f_runtime_conf=train_runtime_conf,
                f_work_mode=train_runtime_conf["job_parameters"]["work_mode"],
                f_dsl=json_loads(pipeline.train_dsl),
                f_migrated=1
            )

        return (0, "Migrating model successfully. " \
                  "The configuration of model has been modified automatically. " \
                  "New model id is: {}, model version is: {}. " \
                  "Model files can be found at '{}'.".format(train_runtime_conf["job_parameters"]["model_id"],
                                                             migrate_model.model_version,
                                                             migrate_model.model_path),
                {"model_id": migrate_model.model_id,
                 "model_version": migrate_model.model_version,
                 "path": migrate_model.model_path})

    except Exception as e:
        return 100, str(e), {}