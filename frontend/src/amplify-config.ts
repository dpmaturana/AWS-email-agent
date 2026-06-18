/**
 * Amplify configuration.
 * After CDK deploy, copy UserPoolId and UserPoolClientId from the CDK outputs
 * into the environment variables below (or into a .env.local file).
 *
 * VITE_COGNITO_USER_POOL_ID=eu-west-1_XXXXXXXXX
 * VITE_COGNITO_CLIENT_ID=XXXXXXXXXXXXXXXXXXXXXXXXXX
 * VITE_API_URL=https://XXXXXXXXXX.execute-api.eu-west-1.amazonaws.com/prod
 */

export const amplifyConfig = {
  Auth: {
    Cognito: {
      userPoolId:       import.meta.env.VITE_COGNITO_USER_POOL_ID as string,
      userPoolClientId: import.meta.env.VITE_COGNITO_CLIENT_ID    as string,
      loginWith: {
        email: true,
      },
    },
  },
}
