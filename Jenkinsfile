LIB_NAME = 'PyHive'
String currentVersion = ""

podTemplate(
    imagePullSecrets: ['preset-pull'],
    nodeUsageMode: 'NORMAL',
    containers: [
        containerTemplate(
            alwaysPullImage: true,
            name: 'ci',
            image: 'preset/ci:latest',
            ttyEnabled: true,
            command: 'cat',
            resourceRequestCpu: '100m',
            resourceLimitCpu: '200m',
            resourceRequestMemory: '1000Mi',
            resourceLimitMemory: '2000Mi',
        ),
        containerTemplate(
            alwaysPullImage: true,
            name: 'py-ci',
            image: 'preset/python:3.10.13-2024-02-21-ci',
            ttyEnabled: true,
            command: 'cat'
        )
    ]
) {
    node(POD_LABEL) {
        container('py-ci') {
            stage('Checkout') {
                checkout scm
            }

            stage('Install System Dependencies') {
                sh 'apt-get update && apt-get install -y libkrb5-dev python3-dev libsasl2-dev'
            }

            stage('Tests') {
                sh(script: 'pip install -e . && pip install -r dev_requirements.txt', label: 'install dependencies')
                parallel(
                    check: {
                        currentVersion = sh(
                                script: "python setup.py --version",
                                returnStdout: true,
                                label: 'Get current version'
                        ).trim()
                        def retVal = sh(
                                script: "curl -I -f https://pypi.devops.preset.zone/${LIB_NAME}/${LIB_NAME}-${currentVersion}.tar.gz",
                                returnStatus: true,
                                label: 'Check for existing tarball'
                        )
                        // If the thing exists, we should bail as we don't want to overwrite
                        if (retVal == 0) {
                            error("Version ${currentVersion} of ${LIB_NAME} already exists! Version bump required.")
                        }
                    }
                )
            }
        }

        container('py-ci') {
            stage('Package Release') {
                if (env.BRANCH_NAME.startsWith("PR-")) {
                    sh(script:"git config --global --add safe.directory /home/jenkins/agent/workspace/preset-io_PyHive_${env.BRANCH_NAME}", label: 'Setting safe directory')
                    def shortGitRev = sh(
                            returnStdout: true,
                            script: 'git rev-parse --short HEAD'
                    ).trim()
                    def pullRequestVersion = "${currentVersion}+${env.BRANCH_NAME}.${shortGitRev}"
                    sh(script:"sed -i 's/__version__ = \"${currentVersion}\"/__version__ = \"${pullRequestVersion}\"/g' pyhive/__init__.py", label: 'Changing version for PR')
                    sh(script:"echo PR version: ${pullRequestVersion}", label: 'PR Release candidate version')
                }
                sh(script: 'python setup.py sdist --formats=gztar', label: 'Bundling release')
                sh(script: "mkdir -p dist/${LIB_NAME} && mv dist/*.gz dist/${LIB_NAME}", label: 'Setup release folder')
            }
        }

        container('ci') {
            stage('Upload Release') {
                withCredentials([
                    [
                        $class           : 'AmazonWebServicesCredentialsBinding',
                        credentialsId    : 'ci-user',
                        accessKeyVariable: 'AWS_ACCESS_KEY_ID',
                        secretKeyVariable: 'AWS_SECRET_ACCESS_KEY',
                    ]
                ]) {
                    if ((env.BRANCH_NAME == 'master') || (env.BRANCH_NAME.startsWith("PR-"))) {
                        sh(script: "aws s3 sync ./dist s3://preset-pypi", label: "Uploading to s3")
                    }
                    else {
                        echo "Skipping upload as this isn't master..."
                    }
                }
            }
        }
    }
}
