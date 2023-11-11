def userEntity(user) -> dict:
    return{"id": str(user["_id"]),
            "name":user["fullname"],
            "email":user["email"],
            "password": user["password"],
            "tenantdb":user["tenantdb"],
            "application":user["application"],
            "status":user["status"],
            "createdate":user["createdate"],
            "imageUrl": str(user["imageUrl"]),

    }

def usersEntities(entities) -> list:
    return [userEntity(item) for item in entities]