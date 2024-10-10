```swift
import Cocoa

class SoftwareEngineer {
    let name = "Andrea Moleri"
    let role = "Software Engineer"
    let spokenLanguages = ["🇮🇹", "🇺🇸", "🇪🇸", "🇩🇪"]
    let areasOfInterest = ["Federated Learning", "Queue Theory", "Large Scale Crowd Management"]
    let specializations = ["Machine Learning", "Data Analysis", "iOS Development"]
    
    func greetings() {
        print("Hi there, thanks for dropping by! Hope you find something interesting")
    }
}

let me = SoftwareEngineer()
me.greetings()
```
